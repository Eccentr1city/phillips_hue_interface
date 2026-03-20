"""Streaming engine using hue-entertainment-pykit for real-time DTLS effects."""

import os
import signal
import sys
import time
import traceback
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
    for _ in range(30):  # up to 3 seconds
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
    time.sleep(0.5)
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
    """Run the DTLS streaming render loop (blocking). Called in the forked subprocess."""
    import hue_entertainment_pykit as hep

    _write_pid()
    _log(f"Stream child started (pid={os.getpid()})")

    def _cleanup(signum, frame):
        _log("Received SIGTERM, exiting")
        _clear_pid()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _cleanup)
    signal.signal(signal.SIGINT, _cleanup)

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
            for light_id, effect_info in light_effects.items():
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
    """Fork a background process to run streaming effects.

    Returns:
        PID of the forked process, or None if no effects.
    """
    from hue.effects import get_effect

    light_effects: dict[int, dict] = {}
    for light_id_str, config in scene_data.get("lights", {}).items():
        if "effect" in config:
            effect_name = config["effect"]
            eff = get_effect(effect_name)
            light_effects[int(light_id_str)] = {
                "render": eff["render"],
                "params": config.get("params", {}),
            }

    if not light_effects:
        return None

    # Ignore SIGCHLD so forked children don't become zombies
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)

    pid = os.fork()
    if pid == 0:
        # Child process — detach from parent
        try:
            os.setsid()
            run_stream_loop(bridge_ip, api_key, client_key, light_effects)
        except Exception:
            _log(f"Fork child exception: {traceback.format_exc()}")
        finally:
            os._exit(0)
    else:
        PID_FILE.write_text(str(pid))
        return pid
