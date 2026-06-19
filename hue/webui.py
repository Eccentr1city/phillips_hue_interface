"""Web control panel — live sliders for effects and beat-sync.

`hue ui` starts a small local Flask server. The page lists every parameterized
program (effects + beatsync), renders sliders from each one's param spec, and
applies changes live: effects hot-reload their daemon, beatsync updates its
in-process engine. Start/Stop and a device picker for beatsync are included.
"""

import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_file

from hue import score as score_mod
from hue.audiosync import BEATENV_PY, PROJECT_DIR
from hue.audiosync import DEFAULTS as BEAT_DEFAULTS
from hue.audiosync import PARAMS as BEAT_PARAMS
from hue.audiosync import BeatSyncEngine, input_devices
from hue.audiosync import _COLORS as COLORS

app = Flask(__name__)
_engine = BeatSyncEngine()
_bridge = None
_scores = {}  # score_id -> light score
_audio = {}  # score_id -> fetched audio file path (for browser playback)
_layout = None  # cached light layout (positions + names)
_VIZ_DIR = os.path.join(tempfile.gettempdir(), "hue_viz")
os.makedirs(_VIZ_DIR, exist_ok=True)


def _get_bridge():
    global _bridge
    if _bridge is None:
        from hue.bridge import Bridge

        _bridge = Bridge()
    return _bridge


def _stop_all():
    from hue.smooth import stop_smooth
    from hue.stream import stop_stream

    _engine.stop()
    stop_stream()
    stop_smooth()


def _beat_program():
    params = []
    for spec in BEAT_PARAMS:
        spec = dict(spec)
        spec.setdefault("type", "float")
        spec.setdefault("label", spec["name"])
        spec["default"] = BEAT_DEFAULTS.get(spec["name"])
        params.append(spec)
    return {"id": "beatsync", "kind": "beatsync", "name": "beatsync (music)", "params": params}


@app.get("/api/state")
def state():
    from hue.effects import list_effects

    programs = []
    for eff in list_effects():
        if not eff["params"]:
            continue  # nothing to tune
        programs.append(
            {
                "id": f"effect:{eff['name']}",
                "kind": "effect",
                "name": eff["name"],
                "mode": eff.get("mode") or "streaming",
                "params": eff["params"],
            }
        )
    programs.append(_beat_program())
    return jsonify(
        {
            "programs": programs,
            "devices": input_devices(),
            "colors": list(COLORS.keys()),
            "running": "beatsync" if _engine.is_running() else None,
            "bpm": round(_engine.bpm, 1),
            "locked": _engine.locked,
            "error": _engine.error,
        }
    )


@app.post("/api/effect")
def run_effect():
    from hue.smooth import start_smooth, stop_smooth
    from hue.stream import start_stream, stop_stream

    data = request.get_json(force=True)
    name = data["name"]
    mode = data.get("mode", "streaming")
    params = data.get("params", {})

    _engine.stop()  # beatsync and effects can't share the bridge session
    b = _get_bridge()
    scene = {
        "lights": {str(lt.id): {"effect": name, "params": params} for lt in b.resolve_lights("all")}
    }
    if mode == "smooth":
        stop_stream()
        pid = start_smooth(b.ip, b.api_key, b.client_key, scene)
    else:
        stop_smooth()
        pid = start_stream(b.ip, b.api_key, b.client_key, scene)
    return jsonify({"ok": True, "pid": pid})


@app.post("/api/beatsync")
def run_beat():
    data = request.get_json(force=True)
    device = data.get("device") or None
    params = data.get("params", {})
    _engine.start(device=device, **params)
    return jsonify({"ok": True, "error": _engine.error})


@app.post("/api/stop")
def stop():
    _stop_all()
    return jsonify({"ok": True})


@app.get("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")


def _ensure_layout():
    # From positions.json — no bridge needed, so the visualizer works away from home.
    global _layout
    if _layout is None:
        _layout = score_mod.build_layout()
    return _layout


def _run_analyzer(audio_path):
    """Run the offline analyzer (.beatenv madmom) on a file; return a score dict."""
    if not BEATENV_PY.exists():
        raise RuntimeError("Run `hue beatsetup` first (.beatenv missing).")
    out = audio_path + ".score.json"
    proc = subprocess.run(
        [str(BEATENV_PY), str(PROJECT_DIR / "analyze_offline.py"), audio_path, out],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or not os.path.exists(out):
        raise RuntimeError((proc.stderr or "analysis failed")[-300:])
    score = score_mod.load_score(out)
    os.unlink(out)
    return score


def _score_params():
    params = []
    for spec in score_mod.PARAMS:
        spec = dict(spec)
        spec.setdefault("type", "float")
        spec.setdefault("label", spec["name"])
        spec["default"] = score_mod.DEFAULTS.get(spec["name"])
        params.append(spec)
    return params


def _score_response(sid, score, **extra):
    return jsonify(
        {
            "score_id": sid,
            "layout": _ensure_layout(),
            "params": _score_params(),
            "colors": list(COLORS.keys()),
            "duration": score["duration"],
            "tempo": score["tempo"],
            "beats_per_bar": score["beats_per_bar"],
            **extra,
        }
    )


@app.post("/api/analyze")
def analyze():
    """Analyze an uploaded audio file (the browser plays its own local copy)."""
    f = request.files.get("audio")
    if f is None:
        return jsonify({"error": "no audio uploaded"}), 400
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=Path(f.filename or "x.wav").suffix)
    f.save(tmp.name)
    tmp.close()
    try:
        score = _run_analyzer(tmp.name)
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        try:
            os.unlink(tmp.name)  # discard audio; keep only the score
        except OSError:
            pass
    sid = uuid.uuid4().hex[:8]
    _scores[sid] = score
    return _score_response(sid, score)


@app.post("/api/fetch")
def fetch():
    """Fetch a song's audio from YouTube (yt-dlp), analyze it, serve it back.

    Never touches Spotify, so there's no account/ban risk. A Spotify link is
    resolved to its title via the public oEmbed endpoint, then searched.
    """
    query = (request.get_json(force=True).get("query") or "").strip()
    if not query:
        return jsonify({"error": "empty query"}), 400
    if "open.spotify.com" in query:
        try:
            import requests as rq

            title = rq.get(
                "https://open.spotify.com/oembed", params={"url": query}, timeout=8
            ).json().get("title")
            if title:
                query = title
        except Exception:
            pass
    base = os.path.join(_VIZ_DIR, uuid.uuid4().hex[:8])
    opts = {
        "format": "bestaudio/best",
        "outtmpl": base + ".%(ext)s",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "default_search": "ytsearch1",
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ],
    }
    import yt_dlp

    info, last = None, None
    for _ in range(3):  # YouTube occasionally throttles; retry transient failures
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(query, download=True)
            break
        except Exception as exc:
            last = exc
            time.sleep(1.5)
    if info is None:
        return jsonify({"error": "fetch failed: " + str(last)[-200:]}), 500
    if "entries" in info:
        info = info["entries"][0] if info.get("entries") else {}
    mp3 = base + ".mp3"
    if not os.path.exists(mp3):
        return jsonify({"error": "no audio downloaded"}), 500
    try:
        score = _run_analyzer(mp3)
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500
    sid = uuid.uuid4().hex[:8]
    _scores[sid] = score
    _audio[sid] = mp3
    return _score_response(sid, score, audio_url=f"/audio/{sid}", title=info.get("title", query))


@app.get("/audio/<sid>")
def audio(sid):
    path = _audio.get(sid)
    if not path or not os.path.exists(path):
        return ("not found", 404)
    return send_file(path, mimetype="audio/mpeg")


@app.post("/api/timeline")
def timeline():
    data = request.get_json(force=True)
    score = _scores.get(data.get("score_id"))
    if not score:
        return jsonify({"error": "unknown score_id"}), 404
    fps = 30
    frames = score_mod.render_timeline(score, data.get("params", {}), _ensure_layout(), fps)
    return jsonify({"fps": fps, "frames": frames})


@app.get("/viz")
def viz():
    return Response(VIZ_HTML, mimetype="text/html")


def serve(port=8765):
    url = f"http://127.0.0.1:{port}"
    print(f"Hue control panel: {url}")
    print(f"Song visualizer:   {url}/viz   (Ctrl-C to stop)")
    app.run(host="127.0.0.1", port=port, threaded=True)


INDEX_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Hue control panel</title>
<style>
 body{font-family:-apple-system,system-ui,sans-serif;max-width:640px;margin:24px auto;padding:0 16px;background:#111;color:#eee}
 h1{font-size:20px} select,input[type=text]{background:#222;color:#eee;border:1px solid #444;border-radius:6px;padding:6px}
 .row{margin:14px 0} label{display:block;font-size:13px;color:#aaa;margin-bottom:4px}
 input[type=range]{width:100%} .val{float:right;color:#fff;font-variant-numeric:tabular-nums}
 button{font-size:15px;padding:8px 16px;border-radius:8px;border:0;cursor:pointer;margin-right:8px}
 #start{background:#2d7;color:#012} #stop{background:#e55;color:#fff}
 #status{font-size:13px;color:#9c9;min-height:18px;margin-top:10px}
 .err{color:#f88!important}
</style></head><body>
<h1>💡 Hue control panel <a href="/viz" style="font-size:13px">(song visualizer)</a></h1>
<div class="row"><label>Program</label><select id="prog"></select></div>
<div class="row" id="modeRow" style="display:none"><label>Mode</label>
 <select id="mode"><option value="streaming">streaming (fast)</option><option value="smooth">smooth (REST fades)</option></select></div>
<div class="row" id="devRow" style="display:none"><label>Audio device</label><select id="dev"></select></div>
<div id="controls"></div>
<div class="row"><button id="start">Start / Apply</button><button id="stop">Stop</button></div>
<div id="status"></div>
<script>
let S=null, progs={}, vals={}, activeId=null, timer=null;
const $=id=>document.getElementById(id);
async function load(){
  S=await (await fetch('/api/state')).json();
  progs={}; const sel=$('prog'); sel.innerHTML='';
  S.programs.forEach(p=>{progs[p.id]=p; const o=document.createElement('option');o.value=p.id;o.textContent=p.name;sel.appendChild(o);
    if(!(p.id in vals)){vals[p.id]={}; p.params.forEach(pp=>vals[p.id][pp.name]=pp.default);}});
  const dv=$('dev'); dv.innerHTML='<option value="">(default input)</option>';
  S.devices.forEach(d=>{const o=document.createElement('option');o.value=d.name;o.textContent='['+d.index+'] '+d.name;dv.appendChild(o);
    if(d.name.toLowerCase().includes('blackhole'))o.selected=true;});
  render(); status();
}
function cur(){return progs[$('prog').value];}
function render(){
  const p=cur(); $('modeRow').style.display=p.kind==='effect'?'':'none';
  $('devRow').style.display=p.kind==='beatsync'?'':'none';
  if(p.kind==='effect')$('mode').value=p.mode;
  const c=$('controls'); c.innerHTML='';
  p.params.forEach(pp=>{
    const v=vals[p.id][pp.name];
    const row=document.createElement('div');row.className='row';
    if(pp.type==='color'){
      row.innerHTML='<label>'+pp.label+'</label>';
      const s=document.createElement('select');S.colors.forEach(col=>{const o=document.createElement('option');o.value=col;o.textContent=col;if(col===v)o.selected=true;s.appendChild(o);});
      s.onchange=()=>{vals[p.id][pp.name]=s.value;live();};row.appendChild(s);
    }else if(pp.type==='text'){
      row.innerHTML='<label>'+pp.label+'</label>';
      const t=document.createElement('input');t.type='text';t.value=v||'';t.style.width='100%';
      t.oninput=()=>{vals[p.id][pp.name]=t.value;live();};row.appendChild(t);
    }else{
      const lab=document.createElement('label');lab.innerHTML=pp.label+'<span class="val" id="v_'+pp.name+'">'+(+v).toFixed(2)+'</span>';
      const r=document.createElement('input');r.type='range';r.min=pp.min;r.max=pp.max;r.step=pp.step;r.value=v;
      r.oninput=()=>{vals[p.id][pp.name]=parseFloat(r.value);$('v_'+pp.name).textContent=parseFloat(r.value).toFixed(2);live();};
      row.appendChild(lab);row.appendChild(r);
    }
    c.appendChild(row);
  });
}
function payload(){const p=cur();return p.kind==='effect'
  ?{url:'/api/effect',body:{name:p.name,mode:$('mode').value,params:vals[p.id]}}
  :{url:'/api/beatsync',body:{device:$('dev').value,params:vals[p.id]}};}
async function apply(){const {url,body}=payload();const r=await (await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();status(r.error);}
function live(){if(activeId!==cur().id)return;clearTimeout(timer);timer=setTimeout(apply,150);}
function status(err){const s=$('status');if(err){s.textContent='⚠ '+err;s.className='err';}else{s.className='';s.textContent=activeId?('▶ running: '+activeId):'idle';}}
$('prog').onchange=render;
$('start').onclick=()=>{activeId=cur().id;apply();status();};
$('stop').onclick=async()=>{activeId=null;await fetch('/api/stop',{method:'POST'});status();};
setInterval(async()=>{
  if(activeId!=='beatsync')return;
  const st=await (await fetch('/api/state')).json();
  if(st.error){status(st.error);return;}
  const s=$('status');s.className='';
  s.textContent='▶ beatsync — '+(st.locked?('♪ '+st.bpm+' BPM'):'listening / warming up…');
},1000);
load();
</script></body></html>
"""


VIZ_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Hue visualizer</title>
<style>
 body{font-family:-apple-system,system-ui,sans-serif;max-width:760px;margin:20px auto;padding:0 16px;background:#0c0c0c;color:#eee}
 h1{font-size:20px} a{color:#6cf}
 #cv{width:100%;background:#000;border-radius:12px;display:block;margin:10px 0}
 audio{width:100%;margin:6px 0}
 .row{margin:10px 0} label{display:block;font-size:13px;color:#aaa;margin-bottom:3px}
 .val{float:right;color:#fff;font-variant-numeric:tabular-nums}
 input[type=range]{width:100%} select,input[type=text],input[type=file]{background:#222;color:#eee;border:1px solid #444;border-radius:6px;padding:6px}
 #status{font-size:13px;color:#9c9;min-height:18px}
</style></head><body>
<h1>🎚️ Hue visualizer <a href="/" style="font-size:13px">(control panel)</a></h1>
<div class="row"><label>Drop in a song you have (wav/mp3/m4a/flac) — analyzed offline, audio stays in your browser</label>
 <input type="file" id="file" accept="audio/*"></div>
<div class="row"><label>…or fetch one you don't have (song name, or YouTube/Spotify link)</label>
 <input type="text" id="q" placeholder="e.g. Daft Punk Get Lucky" style="width:68%">
 <button id="fetch" style="padding:7px 14px;border-radius:6px;border:0;background:#37c;color:#fff;cursor:pointer">Fetch</button></div>
<div id="status">Pick or fetch a song.</div>
<audio id="aud" controls></audio>
<canvas id="cv" width="720" height="380"></canvas>
<div id="ctrls"></div>
<script>
let sid=null, layout=[], fps=30, frames=null, vals={}, params=[], colors=[], timer=null;
const $=id=>document.getElementById(id);
const cv=$('cv'), ctx=cv.getContext('2d'), aud=$('aud');
function applyAnalysis(d, audioSrc){
  if(d.error){$('status').textContent='⚠ '+d.error; return;}
  aud.src=audioSrc; sid=d.score_id; layout=d.layout; params=d.params; colors=d.colors;
  vals={}; params.forEach(p=>vals[p.name]=p.default);
  $('status').textContent=(d.title?('“'+d.title+'” · '):'')+
    `${d.tempo} BPM · ${d.beats_per_bar}/bar · ${d.duration.toFixed(0)}s — press play ▶`;
  renderControls(); fetchTimeline();
}
$('file').onchange=async e=>{
  const file=e.target.files[0]; if(!file)return;
  $('status').textContent='Analyzing (full-song madmom)…';
  const fd=new FormData(); fd.append('audio', file);
  const d=await (await fetch('/api/analyze',{method:'POST',body:fd})).json();
  applyAnalysis(d, URL.createObjectURL(file));
};
$('fetch').onclick=async()=>{
  const q=$('q').value.trim(); if(!q)return;
  $('status').textContent='Fetching + analyzing “'+q+'”… (downloading audio)';
  const d=await (await fetch('/api/fetch',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({query:q})})).json();
  applyAnalysis(d, d.audio_url);
};
async function fetchTimeline(){
  if(!sid)return;
  const r=await fetch('/api/timeline',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({score_id:sid, params:vals})});
  const d=await r.json(); if(d.error){$('status').textContent='⚠ '+d.error; return;}
  fps=d.fps; frames=d.frames;
}
function renderControls(){
  const c=$('ctrls'); c.innerHTML='';
  params.forEach(p=>{
    const v=vals[p.name], row=document.createElement('div'); row.className='row';
    if(p.type==='color'){
      row.innerHTML='<label>'+p.label+'</label>';
      const s=document.createElement('select');
      colors.forEach(col=>{const o=document.createElement('option');o.value=col;o.textContent=col;if(col===v)o.selected=true;s.appendChild(o);});
      s.onchange=()=>{vals[p.name]=s.value;live();}; row.appendChild(s);
    }else if(p.type==='text'){
      row.innerHTML='<label>'+p.label+'</label>';
      const t=document.createElement('input');t.type='text';t.value=v||'';t.style.width='100%';
      t.oninput=()=>{vals[p.name]=t.value;live();}; row.appendChild(t);
    }else{
      const lab=document.createElement('label');lab.innerHTML=p.label+'<span class="val" id="v_'+p.name+'">'+(+v).toFixed(2)+'</span>';
      const r=document.createElement('input');r.type='range';r.min=p.min;r.max=p.max;r.step=p.step;r.value=v;
      r.oninput=()=>{vals[p.name]=parseFloat(r.value);$('v_'+p.name).textContent=parseFloat(r.value).toFixed(2);live();};
      row.appendChild(lab);row.appendChild(r);
    }
    c.appendChild(row);
  });
}
function live(){clearTimeout(timer);timer=setTimeout(fetchTimeline,180);}
function draw(){
  requestAnimationFrame(draw);
  const W=cv.width,H=cv.height;
  ctx.fillStyle='#000';ctx.fillRect(0,0,W,H);
  if(!frames||!layout.length)return;
  const idx=Math.max(0,Math.min(frames.length-1,Math.floor(aud.currentTime*fps)));
  const f=frames[idx];
  layout.forEach((lt,i)=>{
    const c=f[i]||[0,0,0]; const [r,g,b]=c;
    const cx=W/2+lt.x*0.42*W, cy=H/2-lt.y*0.42*H;
    const bright=(r+g+b)/3;
    ctx.shadowColor=`rgb(${r},${g},${b})`; ctx.shadowBlur=12+bright/255*55;
    ctx.fillStyle=`rgb(${r},${g},${b})`;
    ctx.beginPath(); ctx.arc(cx,cy,28,0,7); ctx.fill();
    ctx.shadowBlur=0; ctx.fillStyle='#999'; ctx.font='11px sans-serif'; ctx.textAlign='center';
    ctx.fillText(lt.name, cx, cy+46);
  });
}
draw();
</script></body></html>
"""
