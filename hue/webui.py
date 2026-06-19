"""Web control panel — live sliders for effects and beat-sync.

`hue ui` starts a small local Flask server. The page lists every parameterized
program (effects + beatsync), renders sliders from each one's param spec, and
applies changes live: effects hot-reload their daemon, beatsync updates its
in-process engine. Start/Stop and a device picker for beatsync are included.
"""

from flask import Flask, Response, jsonify, request

from hue.audiosync import DEFAULTS as BEAT_DEFAULTS
from hue.audiosync import PARAMS as BEAT_PARAMS
from hue.audiosync import BeatSyncEngine, input_devices
from hue.audiosync import _COLORS as COLORS

app = Flask(__name__)
_engine = BeatSyncEngine()
_bridge = None


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


def serve(port=8765):
    url = f"http://127.0.0.1:{port}"
    print(f"Hue control panel: {url}  (Ctrl-C to stop)")
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
<h1>💡 Hue control panel</h1>
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
setInterval(async()=>{if(activeId==='beatsync'){const st=await (await fetch('/api/state')).json();if(st.error)status(st.error);}},2500);
load();
</script></body></html>
"""
