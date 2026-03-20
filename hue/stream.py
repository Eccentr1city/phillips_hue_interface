"""Streaming engine wrapping hue-entertainment-pykit for real-time effects."""

import os
import signal
import sys
import time
from pathlib import Path

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
        # Check if process is alive
        os.kill(pid, 0)
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


def run_stream_loop(
    bridge_ip: str,
    api_key: str,
    client_key: str,
    light_effects: dict[int, dict],
    fps: int = 25,
):
    """Run the streaming render loop (blocking). Called in the forked subprocess.

    Args:
        bridge_ip: Bridge IP address.
        api_key: Hue API key (username).
        client_key: Hue entertainment client key.
        light_effects: Mapping of light ID → {"render": callable, "params": dict}.
        fps: Frames per second (default 25).
    """
    try:
        from HueEntertainmentPykit import Entertainment, Streaming
    except ImportError:
        print(
            "hue-entertainment-pykit is required for streaming effects. "
            "Install with: uv add hue-entertainment-pykit",
            file=sys.stderr,
        )
        sys.exit(1)

    _write_pid()

    def _cleanup(signum, frame):
        _clear_pid()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _cleanup)
    signal.signal(signal.SIGINT, _cleanup)

    try:
        # Set up entertainment API connection
        ent = Entertainment(bridge_ip, api_key, client_key)
        ent.create_group(list(light_effects.keys()))
        streaming = Streaming(ent)
        streaming.start()

        interval = 1.0 / fps
        start_time = time.monotonic()

        while True:
            t = time.monotonic() - start_time
            for light_id, effect_info in light_effects.items():
                render_fn = effect_info["render"]
                params = effect_info.get("params", {})
                r, g, b = render_fn(t, **params)
                # hue-entertainment-pykit expects 0-255
                streaming.set_color(light_id, r, g, b)
            streaming.render()
            # Sleep to maintain FPS
            elapsed = time.monotonic() - start_time - t
            sleep_time = interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)
    except Exception as e:
        print(f"Stream error: {e}", file=sys.stderr)
    finally:
        _clear_pid()


def fork_stream(
    bridge_ip: str,
    api_key: str,
    client_key: str,
    scene_data: dict,
):
    """Fork a background process to run streaming effects for a scene.

    Args:
        bridge_ip: Bridge IP address.
        api_key: Hue API key.
        client_key: Hue entertainment client key.
        scene_data: Scene dict with light configs (from scene JSON).

    Returns:
        PID of the forked process.
    """
    from hue.effects import get_effect

    # Resolve effect render functions before forking
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
        # Parent — write PID file for the child
        PID_FILE.write_text(str(pid))
        return pid
