"""Streaming daemon — keeps a single DTLS connection and hot-swaps effects.

The daemon is a long-lived subprocess that owns the DTLS entertainment session.
To change effects, the parent writes a new config JSON and sends SIGUSR1.
To stop, the parent sends SIGTERM. No DTLS teardown/reconnect on effect switch.

Config file format (.hue_stream_config.json):
{
    "bridge_ip": "...",
    "api_key": "...",
    "client_key": "...",
    "light_effects": {"1": {"effect": "candle", "params": {}}, ...}
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

PROJECT_DIR = Path(__file__).resolve().parent.parent
PID_FILE = PROJECT_DIR / ".hue_stream.pid"
LOG_FILE = PROJECT_DIR / ".hue_stream.log"
CONFIG_FILE = PROJECT_DIR / ".hue_stream_config.json"


def _log(msg: str):
    """Append a line to the stream log file for debugging."""
    with open(LOG_FILE, "a") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")


def get_running_pid() -> int | None:
    """Return the PID of the running daemon, or None."""
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


def stop_stream():
    """Stop the streaming daemon."""
    pid = get_running_pid()
    if pid is None:
        return False

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        try:
            PID_FILE.unlink()
        except FileNotFoundError:
            pass
        return True

    # Wait for graceful shutdown
    for _ in range(30):
        try:
            os.kill(pid, 0)
            time.sleep(0.1)
        except ProcessLookupError:
            break

    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass

    try:
        PID_FILE.unlink()
    except FileNotFoundError:
        pass
    return True


def _write_config(bridge_ip: str, api_key: str, client_key: str, light_effects: dict):
    """Write the config JSON that the daemon reads."""
    config_data = {
        "bridge_ip": bridge_ip,
        "api_key": api_key,
        "client_key": client_key,
        "light_effects": light_effects,
    }
    CONFIG_FILE.write_text(json.dumps(config_data))


def _build_light_to_channel_map(bridge_ip: str, api_key: str) -> dict[int, int]:
    """Build a mapping from v1 light IDs to entertainment channel IDs."""
    headers = {"hue-application-key": api_key}
    base = f"https://{bridge_ip}"

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
            channel_to_ent_rid[cid] = members[0]["service"]["rid"]

    light_to_channel: dict[int, int] = {}
    for cid, ent_rid in channel_to_ent_rid.items():
        v1_id = ent_rid_to_v1.get(ent_rid)
        if v1_id is not None:
            light_to_channel[v1_id] = cid

    return light_to_channel


def _resolve_effects(light_effects: dict[int, dict]) -> dict[int, dict]:
    """Resolve effect names to render functions."""
    from hue.effects import get_effect

    render_map: dict[int, dict] = {}
    for light_id, info in light_effects.items():
        eff = get_effect(info["effect"])
        render_map[light_id] = {
            "render": eff["render"],
            "params": info.get("params", {}),
        }
    return render_map


def run_daemon(config_path: str):
    """Main daemon loop — connect once, hot-swap effects via SIGUSR1."""
    import hue_entertainment_pykit as hep

    PID_FILE.write_text(str(os.getpid()))
    _log(f"Daemon started (pid={os.getpid()})")

    # Load initial config
    config_data = json.loads(Path(config_path).read_text())
    bridge_ip = config_data["bridge_ip"]
    api_key = config_data["api_key"]
    client_key = config_data["client_key"]

    light_effects = {
        int(lid): info for lid, info in config_data["light_effects"].items()
    }
    render_map = _resolve_effects(light_effects)

    # SIGUSR1 handler: reload config and swap effects (no DTLS reconnect)
    reload_flag = [False]

    def _on_reload(signum, frame):
        reload_flag[0] = True

    signal.signal(signal.SIGUSR1, _on_reload)

    # Build channel map
    light_to_channel = _build_light_to_channel_map(bridge_ip, api_key)
    _log(f"Channel map: {light_to_channel}")

    # Set up DTLS connection
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
        PID_FILE.unlink(missing_ok=True)
        sys.exit(1)

    config_id = list(configs.keys())[0]
    ent_config = configs[config_id]
    ent_conf_repo = ent.get_ent_conf_repo()

    # SIGTERM handler: clean shutdown (set flag so render loop exits cleanly)
    shutdown_flag = [False]

    def _on_shutdown(signum, frame):
        _log("Received SIGTERM, shutting down")
        shutdown_flag[0] = True

    signal.signal(signal.SIGTERM, _on_shutdown)
    signal.signal(signal.SIGINT, _on_shutdown)

    # Outer loop: auto-reconnect on DTLS errors (bridge reboot, network blip, etc.)
    fps = 25
    interval = 1.0 / fps

    while not shutdown_flag[0]:
        streaming = hep.Streaming(bridge, ent_config, ent_conf_repo)
        streaming.set_color_space("rgb")

        # DTLS handshake with retry
        connected = False
        for attempt in range(5):
            if shutdown_flag[0]:
                break
            try:
                _log(f"DTLS handshake attempt {attempt + 1}")
                streaming.start_stream()
                _log("DTLS stream started")
                connected = True
                break
            except Exception as exc:
                _log(f"DTLS handshake failed: {exc}")
                if attempt < 4:
                    time.sleep(3)

        if not connected:
            if shutdown_flag[0]:
                break
            _log("DTLS handshake failed after 5 attempts, retrying in 10s")
            time.sleep(10)
            continue

        # Render loop — runs until error or shutdown
        start_time = time.monotonic()
        try:
            while not shutdown_flag[0]:
                # Check for config reload
                if reload_flag[0]:
                    reload_flag[0] = False
                    try:
                        new_data = json.loads(CONFIG_FILE.read_text())
                        new_effects = {
                            int(lid): info
                            for lid, info in new_data["light_effects"].items()
                        }
                        render_map = _resolve_effects(new_effects)
                        light_to_channel = _build_light_to_channel_map(
                            bridge_ip, api_key
                        )
                        _log(f"Reloaded effects: {list(render_map.keys())}")
                    except Exception as exc:
                        _log(f"Reload failed: {exc}")

                t = time.monotonic() - start_time
                for light_id, effect_info in render_map.items():
                    channel_id = light_to_channel.get(light_id)
                    if channel_id is None:
                        continue
                    render_fn = effect_info["render"]
                    params = effect_info.get("params", {})
                    if "phase" not in params:
                        params = {**params, "phase": float(channel_id)}
                    r, g, b = render_fn(t, **params)
                    streaming.set_input((r, g, b, channel_id))

                elapsed = time.monotonic() - start_time - t
                sleep_time = interval - elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)
        except Exception as exc:
            _log(f"Render loop error: {exc}, will reconnect")

        try:
            streaming.stop_stream()
        except Exception:
            pass

        if not shutdown_flag[0]:
            _log("Reconnecting in 3s...")
            time.sleep(3)

    # Clean shutdown
    try:
        streaming.stop_stream()
    except Exception:
        pass
    PID_FILE.unlink(missing_ok=True)
    _log("Daemon stopped")


def start_stream(
    bridge_ip: str,
    api_key: str,
    client_key: str,
    scene_data: dict,
) -> int | None:
    """Start or update the streaming daemon.

    If the daemon is already running, hot-swaps effects via SIGUSR1.
    If not running, launches a new daemon subprocess.

    Returns:
        PID of the daemon, or None if no effects in scene_data.
    """
    from hue.effects import get_effect

    # Validate effects and collect config
    light_effects: dict[str, dict] = {}
    for light_id_str, config in scene_data.get("lights", {}).items():
        if "effect" in config:
            effect_name = config["effect"]
            get_effect(effect_name)  # validate
            light_effects[light_id_str] = {
                "effect": effect_name,
                "params": config.get("params", {}),
            }

    if not light_effects:
        return None

    # Write config file
    _write_config(bridge_ip, api_key, client_key, light_effects)

    # If daemon is already running, just signal it to reload
    pid = get_running_pid()
    if pid is not None:
        _log(f"Signaling daemon (pid={pid}) to reload effects")
        os.kill(pid, signal.SIGUSR1)
        return pid

    # Launch new daemon
    python = sys.executable
    proc = subprocess.Popen(
        [python, "-m", "hue.stream", str(CONFIG_FILE)],
        cwd=str(PROJECT_DIR),
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )

    PID_FILE.write_text(str(proc.pid))
    _log(f"Launched daemon (pid={proc.pid})")
    return proc.pid


# Keep old name as alias for compatibility
fork_stream = start_stream


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m hue.stream <config_file.json>", file=sys.stderr)
        sys.exit(1)
    run_daemon(sys.argv[1])
