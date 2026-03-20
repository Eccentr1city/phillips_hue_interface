"""Streaming engine using hue-entertainment-pykit for real-time DTLS effects."""

import os
import signal
import sys
import time
from pathlib import Path

import requests

PID_FILE = Path(__file__).resolve().parent.parent / ".hue_stream.pid"


def _write_pid():
    PID_FILE.write_text(str(os.getpid()))


def _clear_pid():
    if PID_FILE.exists():
        PID_FILE.unlink()


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
    """Kill the running stream process if any."""
    pid = get_running_pid()
    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        _clear_pid()
        return True
    return False


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
    # entertainment_rid -> v1 light id (from id_v1 field like "/lights/2")
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

    # Combine: v1 light id -> channel id
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
    """Run the DTLS streaming render loop (blocking). Called in the forked subprocess.

    Args:
        bridge_ip: Bridge IP address.
        api_key: Hue API key (username).
        client_key: Hue entertainment client key.
        light_effects: Mapping of v1 light ID -> {"render": callable, "params": dict}.
        fps: Frames per second (default 25).
    """
    import hue_entertainment_pykit as hep

    _write_pid()

    def _cleanup(signum, frame):
        _clear_pid()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _cleanup)
    signal.signal(signal.SIGINT, _cleanup)

    # Build light ID -> channel ID mapping
    light_to_channel = _build_light_to_channel_map(bridge_ip, api_key)

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
        print("No entertainment areas configured on bridge.", file=sys.stderr)
        _clear_pid()
        sys.exit(1)

    config_id = list(configs.keys())[0]
    ent_config = configs[config_id]
    ent_conf_repo = ent.get_ent_conf_repo()

    streaming = hep.Streaming(bridge, ent_config, ent_conf_repo)
    streaming.set_color_space("rgb")
    streaming.start_stream()

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
        print(f"Stream error: {exc}", file=sys.stderr)
    finally:
        streaming.stop_stream()
        _clear_pid()


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

    pid = os.fork()
    if pid == 0:
        # Child process
        try:
            run_stream_loop(bridge_ip, api_key, client_key, light_effects)
        except Exception:
            pass
        finally:
            os._exit(0)
    else:
        PID_FILE.write_text(str(pid))
        return pid
