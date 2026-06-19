"""Smooth-mode daemon — drives effects via the REST API with overlapping fades.

An alternative to the entertainment-streaming daemon (hue/stream.py), for slow
ambient effects. Instead of streaming raw frames (which the bridge applies
discretely, so slow fades visibly step), we send REST keyframes with a
transition time LONGER than the keyframe interval. The bulb firmware does the
fade between keyframes, and the overlap blends consecutive eases into continuous
motion — the same buttery fade the phone app gets.

Trade-off vs streaming: the bridge caps REST at roughly 10-15 light commands/sec
total, so this is for slow/medium effects. Fast effects (candle flicker) still
want streaming, where the speed hides the per-frame steps.

Config file (.hue_smooth_config.json):
{
    "bridge_ip": "...", "api_key": "...",
    "interval": 0.4, "transition": 1.0,
    "light_effects": {"1": {"effect": "wave", "params": {...}}, ...}
}
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import requests

requests.packages.urllib3.disable_warnings()

PROJECT_DIR = Path(__file__).resolve().parent.parent
PID_FILE = PROJECT_DIR / ".hue_smooth.pid"
LOG_FILE = PROJECT_DIR / ".hue_smooth.log"
CONFIG_FILE = PROJECT_DIR / ".hue_smooth_config.json"


def _log(msg: str):
    with open(LOG_FILE, "a") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")


def _unlink_pid_if_mine(pid: int):
    """Remove the PID file only if it still names ``pid`` (see hue/stream.py)."""
    try:
        if PID_FILE.read_text().strip() == str(pid):
            PID_FILE.unlink()
    except (FileNotFoundError, ValueError):
        pass


def get_running_pid() -> int | None:
    """Return the PID of the running smooth daemon, or None."""
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        try:
            PID_FILE.unlink()
        except FileNotFoundError:
            pass
        return None


def stop_smooth() -> bool:
    """Stop the smooth daemon. Returns True if one was running."""
    pid = get_running_pid()
    if pid is None:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _unlink_pid_if_mine(pid)
        return True
    died = False
    for _ in range(50):  # up to ~5s
        try:
            os.kill(pid, 0)
            time.sleep(0.1)
        except ProcessLookupError:
            died = True
            break
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass
    if died:
        _unlink_pid_if_mine(pid)
    return True


def rgb_to_xy(r: float, g: float, b: float) -> tuple[float, float]:
    """Convert 0..1 RGB to Hue xy chromaticity (with sRGB gamma)."""

    def lin(c):
        return ((c + 0.055) / 1.055) ** 2.4 if c > 0.04045 else c / 12.92

    r, g, b = lin(r), lin(g), lin(b)
    X = r * 0.649926 + g * 0.103455 + b * 0.197109
    Y = r * 0.234327 + g * 0.743075 + b * 0.022598
    Z = g * 0.053077 + b * 1.035763
    s = X + Y + Z
    if s == 0:
        return 0.0, 0.0
    return X / s, Y / s


def _write_config(bridge_ip, api_key, light_effects, interval, transition):
    CONFIG_FILE.write_text(
        json.dumps(
            {
                "bridge_ip": bridge_ip,
                "api_key": api_key,
                "interval": interval,
                "transition": transition,
                "light_effects": light_effects,
            }
        )
    )


def _build_render_map(ip, api_key, light_effects):
    """Resolve effects to render fns + static kwargs (positions, phase, params)."""
    from hue.effects import get_effect
    from hue.stream import _build_channel_maps, _effect_kwargs

    _, positions = _build_channel_maps(ip, api_key)
    render_map = {}
    for light_id, info in light_effects.items():
        eff = get_effect(info["effect"])
        kwargs = _effect_kwargs(
            eff["render"],
            float(light_id),
            positions.get(light_id),
            info.get("params", {}),
        )
        render_map[light_id] = {"render": eff["render"], "kwargs": kwargs}
    return render_map


def run_daemon(config_path: str):
    """Main loop — send REST keyframes with overlapping transitions."""
    PID_FILE.write_text(str(os.getpid()))
    _log(f"Smooth daemon started (pid={os.getpid()})")

    cfg = json.loads(Path(config_path).read_text())
    ip, api_key = cfg["bridge_ip"], cfg["api_key"]
    interval = cfg.get("interval", 0.4)
    transition = cfg.get("transition", 1.0)
    light_effects = {int(lid): info for lid, info in cfg["light_effects"].items()}
    render_map = _build_render_map(ip, api_key, light_effects)
    _log(f"Effects: {list(render_map)} interval={interval} transition={transition}")

    reload_flag = [False]
    shutdown_flag = [False]
    signal.signal(signal.SIGUSR1, lambda *_: reload_flag.__setitem__(0, True))
    signal.signal(signal.SIGTERM, lambda *_: shutdown_flag.__setitem__(0, True))
    signal.signal(signal.SIGINT, lambda *_: shutdown_flag.__setitem__(0, True))

    sess = requests.Session()
    sess.verify = False
    tt = max(1, int(round(transition * 10)))  # v1 transitiontime, deciseconds

    start = time.monotonic()
    next_frame = start
    while not shutdown_flag[0]:
        if reload_flag[0]:
            reload_flag[0] = False
            try:
                cfg = json.loads(CONFIG_FILE.read_text())
                interval = cfg.get("interval", 0.4)
                transition = cfg.get("transition", 1.0)
                tt = max(1, int(round(transition * 10)))
                light_effects = {
                    int(lid): info for lid, info in cfg["light_effects"].items()
                }
                render_map = _build_render_map(ip, api_key, light_effects)
                _log(f"Reloaded: {list(render_map)} interval={interval} tt={tt}")
            except Exception as exc:
                _log(f"Reload failed: {exc}")

        t = time.monotonic() - start
        for light_id, info in render_map.items():
            r, g, b = info["render"](t, **info["kwargs"])
            cx, cy = rgb_to_xy(r / 255.0, g / 255.0, b / 255.0)
            bri = max(1, min(254, int(max(r, g, b) / 255.0 * 254)))
            try:
                sess.put(
                    f"https://{ip}/api/{api_key}/lights/{light_id}/state",
                    json={"on": True, "bri": bri, "xy": [cx, cy], "transitiontime": tt},
                    timeout=2,
                )
            except Exception as exc:
                _log(f"PUT light {light_id} failed: {exc}")

        next_frame += interval
        sleep_time = next_frame - time.monotonic()
        if sleep_time > 0:
            time.sleep(sleep_time)
        else:
            next_frame = time.monotonic()

    _unlink_pid_if_mine(os.getpid())
    _log("Smooth daemon stopped")


def start_smooth(
    bridge_ip, api_key, client_key, scene_data, interval=0.4, transition=1.0
) -> int | None:
    """Start or hot-reload the smooth daemon for the effects in scene_data.

    ``client_key`` is unused (REST needs no DTLS) but kept for a signature
    parallel to hue.stream.start_stream.
    """
    from hue.effects import get_effect

    light_effects = {}
    for lid_str, conf in scene_data.get("lights", {}).items():
        if "effect" in conf:
            get_effect(conf["effect"])  # validate
            light_effects[lid_str] = {
                "effect": conf["effect"],
                "params": conf.get("params", {}),
            }
    if not light_effects:
        return None

    _write_config(bridge_ip, api_key, light_effects, interval, transition)

    pid = get_running_pid()
    if pid is not None:
        os.kill(pid, signal.SIGUSR1)
        return pid

    proc = subprocess.Popen(
        [sys.executable, "-m", "hue.smooth", str(CONFIG_FILE)],
        cwd=str(PROJECT_DIR),
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    PID_FILE.write_text(str(proc.pid))
    return proc.pid


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m hue.smooth <config_file.json>", file=sys.stderr)
        sys.exit(1)
    run_daemon(sys.argv[1])
