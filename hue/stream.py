"""Streaming engine using hue-entertainment-pykit for real-time DTLS effects."""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests

PID_FILE = Path(__file__).resolve().parent.parent / ".hue_stream.pid"
LOG_FILE = Path(__file__).resolve().parent.parent / ".hue_stream.log"


def _write_pid():
    PID_FILE.write_text(str(os.getpid()))


def _clear_pid():
    if PID_FILE.exists():
        PID_FILE.unlink()


def _log(msg: str):
    """Append a line to the stream log file for debugging."""
    with open(LOG_FILE, "a") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")


def get_running_pid() -> int | None:
    """Return the PID of the running stream process, or None."""
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)  # check alive
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        _clear_pid()
        return None


def stop_stream():
    """Kill the running stream process and wait for it to exit."""
    pid = get_running_pid()
    if pid is None:
        return False

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _clear_pid()
        return True

    # Wait for the process to actually die so the bridge releases the DTLS session
    dead = False
    for _ in range(20):  # up to 2 seconds for graceful shutdown
        try:
            os.kill(pid, 0)
            time.sleep(0.1)
        except ProcessLookupError:
            dead = True
            break

    # Escalate to SIGKILL if SIGTERM wasn't enough
    if not dead:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        for _ in range(10):  # up to 1 more second
            try:
                os.kill(pid, 0)
                time.sleep(0.1)
            except ProcessLookupError:
                break

    # Reap zombie if we're the parent
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass

    _clear_pid()
    # Give the bridge a moment to fully release the session
    time.sleep(2.0)
    return True


def _build_light_to_channel_map(bridge_ip: str, api_key: str) -> dict[int, int]:
    """Build a mapping from v1 light IDs to entertainment channel IDs.

    Queries the v2 API to correlate entertainment services, light services,
    and channel assignments in the entertainment configuration.
    """
    headers = {"hue-application-key": api_key}
    base = f"https://{bridge_ip}"

    # Get entertainment services: maps entertainment_rid -> v1 light id
    resp = requests.get(
        f"{base}/clip/v2/resource/entertainment",
        headers=headers,
        verify=False,
        timeout=10,
    )
    ent_services = resp.json().get("data", [])
    ent_rid_to_v1: dict[str, int] = {}
    for svc in ent_services:
        v1 = svc.get("id_v1", "")
        if v1.startswith("/lights/"):
            ent_rid_to_v1[svc["id"]] = int(v1.split("/")[-1])

    # Get entertainment configuration: maps channel_id -> entertainment_rid
    resp = requests.get(
        f"{base}/clip/v2/resource/entertainment_configuration",
        headers=headers,
        verify=False,
        timeout=10,
    )
    configs = resp.json().get("data", [])
    if not configs:
        return {}

    config = configs[0]
    channel_to_ent_rid: dict[int, str] = {}
    for channel in config.get("channels", []):
        cid = channel["channel_id"]
        members = channel.get("members", [])
        if members:
            ent_rid = members[0]["service"]["rid"]
            channel_to_ent_rid[cid] = ent_rid

    light_to_channel: dict[int, int] = {}
    for cid, ent_rid in channel_to_ent_rid.items():
        v1_id = ent_rid_to_v1.get(ent_rid)
        if v1_id is not None:
            light_to_channel[v1_id] = cid

    return light_to_channel


def run_stream_loop(
    bridge_ip: str,
    api_key: str,
    client_key: str,
    light_effects: dict[int, dict],
    fps: int = 25,
):
    """Run the DTLS streaming render loop (blocking). Called in the subprocess.

    light_effects maps light ID (int) to {"effect": "name", "params": {...}}.
    Effect render functions are resolved here in the child process.
    """
    import hue_entertainment_pykit as hep

    from hue.effects import get_effect

    _write_pid()
    _log(f"Stream child started (pid={os.getpid()})")

    # Placeholder — will be replaced with a closure once streaming object exists
    signal.signal(signal.SIGINT, lambda s, f: os._exit(0))
    signal.signal(signal.SIGTERM, lambda s, f: os._exit(0))

    # Resolve effect names to render functions
    render_map: dict[int, dict] = {}
    for light_id, info in light_effects.items():
        eff = get_effect(info["effect"])
        render_map[light_id] = {
            "render": eff["render"],
            "params": info.get("params", {}),
        }

    # Build light ID -> channel ID mapping
    light_to_channel = _build_light_to_channel_map(bridge_ip, api_key)
    _log(f"Channel map: {light_to_channel}")

    bridge = hep.create_bridge(
        identification="",
        rid="",
        ip_address=bridge_ip,
        swversion=0,
        username=api_key,
        hue_app_id="phillips_hue_interface",
        clientkey=client_key,
        name="Hue Bridge",
    )

    ent = hep.Entertainment(bridge)
    configs = ent.get_entertainment_configs()
    if not configs:
        _log("ERROR: No entertainment areas configured on bridge")
        _clear_pid()
        sys.exit(1)

    config_id = list(configs.keys())[0]
    ent_config = configs[config_id]
    ent_conf_repo = ent.get_ent_conf_repo()

    streaming = hep.Streaming(bridge, ent_config, ent_conf_repo)
    streaming.set_color_space("rgb")

    # Now install the real signal handler that can close the DTLS session
    def _cleanup(signum, frame):
        _log("Received SIGTERM, stopping stream")
        _clear_pid()
        # Run stop_stream in a thread with a hard timeout — it can hang
        t = threading.Thread(target=lambda: streaming.stop_stream(), daemon=True)
        t.start()
        t.join(timeout=1.0)
        _log("Stream stop attempted, exiting")
        os._exit(0)

    signal.signal(signal.SIGTERM, _cleanup)
    signal.signal(signal.SIGINT, _cleanup)

    # Retry DTLS handshake — bridge may need a moment after previous session
    for attempt in range(3):
        try:
            _log(f"DTLS handshake attempt {attempt + 1}")
            streaming.start_stream()
            _log("DTLS stream started")
            break
        except Exception as exc:
            _log(f"DTLS handshake failed: {exc}")
            if attempt < 2:
                time.sleep(2)
            else:
                _log("DTLS handshake failed after 3 attempts, giving up")
                _clear_pid()
                sys.exit(1)

    try:
        interval = 1.0 / fps
        start_time = time.monotonic()

        while True:
            t = time.monotonic() - start_time
            for light_id, effect_info in render_map.items():
                channel_id = light_to_channel.get(light_id)
                if channel_id is None:
                    continue
                render_fn = effect_info["render"]
                params = effect_info.get("params", {})
                # Auto-inject phase per channel so effects vary per light
                if "phase" not in params:
                    params = {**params, "phase": float(channel_id)}
                r, g, b = render_fn(t, **params)
                streaming.set_input((r, g, b, channel_id))
            elapsed = time.monotonic() - start_time - t
            sleep_time = interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)
    except Exception as exc:
        _log(f"Stream loop error: {exc}")
    finally:
        try:
            streaming.stop_stream()
        except Exception:
            pass
        _clear_pid()
        _log("Stream child exited")


def fork_stream(
    bridge_ip: str,
    api_key: str,
    client_key: str,
    scene_data: dict,
):
    """Launch a background subprocess to run streaming effects.

    Uses subprocess.Popen instead of os.fork() to get a clean process,
    avoiding inherited socket/TLS state from the parent.

    Returns:
        PID of the subprocess, or None if no effects.
    """
    from hue.effects import get_effect

    # Validate effects exist and collect config
    light_effects: dict[str, dict] = {}
    for light_id_str, config in scene_data.get("lights", {}).items():
        if "effect" in config:
            effect_name = config["effect"]
            get_effect(effect_name)  # validate it exists
            light_effects[light_id_str] = {
                "effect": effect_name,
                "params": config.get("params", {}),
            }

    if not light_effects:
        return None

    # Write config to a temp JSON file for the subprocess to read
    config_data = {
        "bridge_ip": bridge_ip,
        "api_key": api_key,
        "client_key": client_key,
        "light_effects": light_effects,
    }
    config_file = PID_FILE.parent / ".hue_stream_config.json"
    config_file.write_text(json.dumps(config_data))

    # Find the Python interpreter in the same environment
    python = sys.executable

    # Launch as a completely new process
    proc = subprocess.Popen(
        [python, "-m", "hue.stream", str(config_file)],
        cwd=str(PID_FILE.parent),
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )

    PID_FILE.write_text(str(proc.pid))
    return proc.pid


if __name__ == "__main__":
    # Entry point for subprocess-based streaming.
    # Usage: python -m hue.stream <config_file.json>
    if len(sys.argv) < 2:
        print("Usage: python -m hue.stream <config_file.json>", file=sys.stderr)
        sys.exit(1)

    config_path = Path(sys.argv[1])
    try:
        config_data = json.loads(config_path.read_text())
    except Exception as exc:
        _log(f"Failed to read config file {config_path}: {exc}")
        sys.exit(1)

    # Parse light_effects: keys are string light IDs
    light_effects = {
        int(lid): info for lid, info in config_data["light_effects"].items()
    }

    run_stream_loop(
        bridge_ip=config_data["bridge_ip"],
        api_key=config_data["api_key"],
        client_key=config_data["client_key"],
        light_effects=light_effects,
    )
