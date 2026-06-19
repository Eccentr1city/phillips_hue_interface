"""Light-score rendering — turn an analyzed song into per-light frames.

A "light score" (produced offline by analyze_offline.py) is a timeline of beats
with bar positions. This module renders it into light states at any moment, so we
can both (a) preview it in the browser visualizer and (b) later drive the real
lights synced to playback position — from the SAME code, so the preview mirrors
the room exactly.

The render is deterministic in time: at time t it finds the most recent beat and
pulses from it, accenting the downbeat ("1"). Anchor lights carry the beat;
flavor lights pulse every beat in their own color. Spatial layout comes from the
measured light positions.
"""

import json
import math
from bisect import bisect_right

import requests

from hue.audiosync import PROJECT_DIR, _parse_color, _resolve_light_ids
from hue.stream import _build_channel_maps

requests.packages.urllib3.disable_warnings()


def _normalize(raw):
    """Uniform-scale real-world coords into the -1..1 cube (see apply_positions)."""
    names = list(raw)
    axes = list(zip(*(raw[n] for n in names)))
    mids = [(min(a) + max(a)) / 2 for a in axes]
    scale = 2.0 / (max(max(a) - min(a) for a in axes) or 1.0)
    return {
        n: {"x": (raw[n][0] - mids[0]) * scale, "y": (raw[n][1] - mids[1]) * scale}
        for n in names
    }

FLAVOR_DECAY = 0.14

DEFAULTS = {
    "max_bright": 0.85,
    "min_bright": 0.05,
    "flavor_max": 0.5,
    "decay": 0.30,
    "downbeat_emphasis": 0.5,
    "color": "red",
    "flavor_color": "amber",
    "flavor": "",  # comma list of names/ids; default = interleaved
}

PARAMS = [
    {"name": "max_bright", "label": "Anchor brightness", "min": 0.05, "max": 1.0, "step": 0.01},
    {"name": "flavor_max", "label": "Flavor brightness", "min": 0.0, "max": 1.0, "step": 0.01},
    {"name": "min_bright", "label": "Baseline glow", "min": 0.0, "max": 0.3, "step": 0.01},
    {"name": "decay", "label": "Beat sustain (s)", "min": 0.05, "max": 1.0, "step": 0.01},
    {"name": "downbeat_emphasis", "label": "Downbeat accent", "min": 0.0, "max": 0.9, "step": 0.05},
    {"name": "color", "label": "Anchor color", "type": "color"},
    {"name": "flavor_color", "label": "Flavor color", "type": "color"},
    {"name": "flavor", "label": "Flavor lights (names)", "type": "text"},
]


def load_score(path):
    with open(path) as f:
        return json.load(f)


def build_layout(ip=None, api_key=None):
    """Lights with names + normalized x/y positions.

    Prefers the local positions.json (works offline / away from the bridge — the
    common case for the visualizer). Falls back to the bridge if creds are given
    and positions.json is absent.
    """
    pj = PROJECT_DIR / "positions.json"
    if pj.exists():
        raw = json.loads(pj.read_text()).get("positions", {})
        normed = _normalize(raw)
        return [
            {"id": n, "name": n, "x": round(p["x"], 4), "y": round(p["y"], 4)}
            for n, p in normed.items()
        ]
    if ip and api_key:
        light_to_channel, light_to_position = _build_channel_maps(ip, api_key)
        lights = requests.get(
            f"https://{ip}/api/{api_key}/lights", verify=False, timeout=8
        ).json()
        names = {int(k): v["name"].strip() for k, v in lights.items()}
        return [
            {
                "id": lid,
                "name": names.get(lid, str(lid)),
                "x": light_to_position.get(lid, {}).get("x", 0.0),
                "y": light_to_position.get(lid, {}).get("y", 0.0),
            }
            for lid in sorted(light_to_channel, key=lambda i: light_to_channel[i])
        ]
    return []


def _flavor_ids(flavor_spec, layout):
    if flavor_spec:
        name_to_id = {lt["name"].lower(): lt["id"] for lt in layout}
        return _resolve_light_ids(flavor_spec, name_to_id)
    # default: every other light, so accents are spread around the room
    return {lt["id"] for lt in layout[1::2]}


def render_timeline(score, params, layout, fps=30):
    """Render the whole score to frames: frames[k] = [[r,g,b], ...] per layout light."""
    p = {**DEFAULTS, **(params or {})}
    times = [b["t"] for b in score["beats"]]
    positions = [b["pos"] for b in score["beats"]]
    anchor_rgb = _parse_color(p["color"])
    flavor_rgb = _parse_color(p["flavor_color"])
    flavor = _flavor_ids(p["flavor"], layout)
    mn, mx, fmx = p["min_bright"], p["max_bright"], p["flavor_max"]
    decay, demph = max(0.03, p["decay"]), p["downbeat_emphasis"]

    n_frames = int(score["duration"] * fps) + 1
    frames = []
    for k in range(n_frames):
        t = k / fps
        i = bisect_right(times, t) - 1
        if i < 0:
            ssb, pos = None, None
        else:
            ssb, pos = t - times[i], positions[i]
        frame = []
        for lt in layout:
            if lt["id"] in flavor:
                env = math.exp(-ssb / FLAVOR_DECAY) if ssb is not None else 0.0
                b = min(fmx, mn + (fmx - mn) * env)
                rgb = flavor_rgb
            else:
                amp = 1.0 if pos in (None, 1) else (1.0 - demph)
                env = amp * math.exp(-ssb / decay) if ssb is not None else 0.0
                b = min(mx, mn + (mx - mn) * env)
                rgb = anchor_rgb
            frame.append([int(c * b) for c in rgb])
        frames.append(frame)
    return frames
