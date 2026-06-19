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

import inspect
import json
import os
import signal
import struct
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


def _unlink_pid_if_mine(pid: int):
    """Remove the PID file only if it still names ``pid``.

    Guards against a slow-exiting old daemon deleting a newer daemon's PID file
    during a restart (both share PID_FILE), which would orphan the live daemon
    and let a second one spawn and fight over the bridge's single DTLS slot.
    """
    try:
        if PID_FILE.read_text().strip() == str(pid):
            PID_FILE.unlink()
    except (FileNotFoundError, ValueError):
        pass


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
        _unlink_pid_if_mine(pid)
        return True

    # Wait for graceful shutdown — DTLS teardown can take several seconds.
    died = False
    for _ in range(100):  # up to ~10s
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

    # Only clear the PID file if the daemon actually exited; otherwise leave it
    # so the still-live daemon stays tracked and no second daemon can spawn.
    if died:
        _unlink_pid_if_mine(pid)
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


def _build_channel_maps(
    bridge_ip: str, api_key: str
) -> tuple[dict[int, int], dict[int, dict]]:
    """Map v1 light IDs to entertainment channel IDs and 3D positions.

    Returns:
        (light_to_channel, light_to_position) where position is
        {"x": float, "y": float, "z": float} in the bridge's -1..1 cube.
    """
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
        return {}, {}

    config = configs[0]
    light_to_channel: dict[int, int] = {}
    light_to_position: dict[int, dict] = {}
    for channel in config.get("channels", []):
        members = channel.get("members", [])
        if not members:
            continue
        v1_id = ent_rid_to_v1.get(members[0]["service"]["rid"])
        if v1_id is None:
            continue
        light_to_channel[v1_id] = channel["channel_id"]
        pos = channel.get("position") or {}
        light_to_position[v1_id] = {
            "x": float(pos.get("x", 0.0)),
            "y": float(pos.get("y", 0.0)),
            "z": float(pos.get("z", 0.0)),
        }

    return light_to_channel, light_to_position


def _effect_kwargs(
    render_fn, phase: float, position: dict | None, user_params: dict
) -> dict:
    """Build the static kwargs for an effect's render(), filtered to its signature.

    User params are always passed. The auto-injected ``phase`` and positional
    ``x``/``y``/``z`` are added only if the effect's signature accepts them (by
    name or via **kwargs), so position-naive effects keep working unchanged.
    """
    sig = inspect.signature(render_fn)
    has_var_kw = any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values())
    accepted = set(sig.parameters)

    kwargs = dict(user_params)
    auto = {"phase": phase}
    if position is not None:
        auto["x"] = position["x"]
        auto["y"] = position["y"]
        auto["z"] = position["z"]
    for key, value in auto.items():
        if key in kwargs:
            continue
        if has_var_kw or key in accepted:
            kwargs[key] = value
    return kwargs


def _send_frame(streaming, frame: list[tuple[int, int, int, int]]):
    """Send one DTLS packet containing ALL channels for this frame.

    The pykit ``set_input`` queues each channel and its worker thread sends a
    separate single-channel packet per color — at N lights x F fps that's N*F
    messages/sec, which overruns the bridge's streaming rate and makes lights
    update one-at-a-time (visible stutter). The Hue Entertainment protocol wants
    one message per frame carrying every channel, so we build and send that
    directly via the streaming service's socket and message builder.

    Each channel is 7 bytes: 1-byte channel id + three big-endian uint16 colors.
    ``_last_message`` is updated so the keep-alive thread re-sends a full frame.
    """
    svc = streaming._streaming_service
    channel_data = b""
    for channel_id, r, g, b in frame:
        channel_data += struct.pack(
            ">BHHH",
            channel_id,
            (r * 65535) // 255,
            (g * 65535) // 255,
            (b * 65535) // 255,
        )
    message = svc._build_message(channel_data)
    svc._dtls_service.get_socket().send(message)
    svc._last_message = message


def _resolve_effects(
    light_effects: dict[int, dict],
    light_to_channel: dict[int, int],
    light_to_position: dict[int, dict],
) -> dict[int, dict]:
    """Resolve effect names to render functions with precomputed static kwargs."""
    from hue.effects import get_effect

    render_map: dict[int, dict] = {}
    for light_id, info in light_effects.items():
        eff = get_effect(info["effect"])
        channel_id = light_to_channel.get(light_id)
        phase = float(channel_id) if channel_id is not None else float(light_id)
        kwargs = _effect_kwargs(
            eff["render"],
            phase,
            light_to_position.get(light_id),
            info.get("params", {}),
        )
        render_map[light_id] = {"render": eff["render"], "kwargs": kwargs}
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

    # SIGUSR1 handler: reload config and swap effects (no DTLS reconnect)
    reload_flag = [False]

    def _on_reload(signum, frame):
        reload_flag[0] = True

    signal.signal(signal.SIGUSR1, _on_reload)

    # Build channel + position maps, then resolve effects against them
    light_to_channel, light_to_position = _build_channel_maps(bridge_ip, api_key)
    _log(f"Channel map: {light_to_channel}")
    _log(f"Positions: {light_to_position}")
    render_map = _resolve_effects(light_effects, light_to_channel, light_to_position)

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
        _unlink_pid_if_mine(os.getpid())
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
    # One full-frame packet per frame keeps us within the bridge's streaming rate
    # even at 50fps, so motion is smooth.
    fps = 50
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
                        light_to_channel, light_to_position = _build_channel_maps(
                            bridge_ip, api_key
                        )
                        render_map = _resolve_effects(
                            new_effects, light_to_channel, light_to_position
                        )
                        _log(f"Reloaded effects: {list(render_map.keys())}")
                        _log(f"Positions: {light_to_position}")
                    except Exception as exc:
                        _log(f"Reload failed: {exc}")

                t = time.monotonic() - start_time
                frame = []
                for light_id, effect_info in render_map.items():
                    channel_id = light_to_channel.get(light_id)
                    if channel_id is None:
                        continue
                    r, g, b = effect_info["render"](t, **effect_info["kwargs"])
                    frame.append((channel_id, r, g, b))
                if frame:
                    _send_frame(streaming, frame)

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
    _unlink_pid_if_mine(os.getpid())
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
