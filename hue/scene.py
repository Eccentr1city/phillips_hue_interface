"""Scene definition, save/load, and apply logic."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hue.bridge import Bridge

SCENES_DIR = Path(__file__).resolve().parent.parent / "scenes"


def list_scenes() -> list[dict]:
    """Return all saved scenes as [{name, path, lights}]."""
    scenes = []
    if not SCENES_DIR.exists():
        return scenes
    for p in sorted(SCENES_DIR.glob("*.json")):
        try:
            data = json.loads(p.read_text())
            scenes.append(
                {
                    "name": p.stem,
                    "path": str(p),
                    "lights": data.get("lights", {}),
                }
            )
        except (json.JSONDecodeError, OSError):
            continue
    return scenes


def get_scene(name: str) -> dict:
    """Load a scene by name. Raises KeyError if not found."""
    path = SCENES_DIR / f"{name}.json"
    if not path.exists():
        raise KeyError(f"No scene named '{name}'")
    return json.loads(path.read_text())


def save_scene(name: str, lights: dict) -> Path:
    """Save a scene definition to JSON.

    Args:
        name: Scene name (becomes the filename).
        lights: Dict mapping light IDs (as strings) to configs.

    Returns:
        Path to the saved scene file.
    """
    SCENES_DIR.mkdir(parents=True, exist_ok=True)
    data = {"name": name, "lights": lights}
    path = SCENES_DIR / f"{name}.json"
    path.write_text(json.dumps(data, indent=2) + "\n")
    return path


def save_scene_from_current(bridge: Bridge, name: str) -> Path:
    """Snapshot the current state of all lights and save as a scene.

    Captures on/off, brightness, hue, saturation, and color temp for each light.
    """
    lights_config: dict[str, dict] = {}
    for lid, light in sorted(bridge.lights.items()):
        state = light.state
        if not state.get("reachable", False):
            continue
        config: dict = {"on": state.get("on", False)}
        if state.get("bri") is not None:
            config["brightness"] = round(state["bri"] / 254, 2)
        # Store raw hue/sat so we can restore exactly
        if state.get("hue") is not None:
            config["hue"] = state["hue"]
        if state.get("sat") is not None:
            config["sat"] = state["sat"]
        if state.get("ct") is not None:
            config["ct"] = state["ct"]
        if state.get("colormode"):
            config["colormode"] = state["colormode"]
        lights_config[str(lid)] = config

    return save_scene(name, lights_config)


def apply_scene(bridge: Bridge, name: str) -> dict:
    """Apply a scene — set static lights via REST and fork streaming for effects.

    Returns:
        {"static": [light_ids], "streaming": [light_ids], "pid": int|None}
    """
    from hue.stream import fork_stream, stop_stream

    scene = get_scene(name)
    lights_config = scene.get("lights", {})

    # Kill any existing stream
    stop_stream()

    static_ids = []
    effect_ids = []

    for light_id_str, config in lights_config.items():
        light_id = int(light_id_str)
        if "effect" in config:
            effect_ids.append(light_id)
            continue

        # Static light — apply via REST
        static_ids.append(light_id)
        try:
            light = bridge.light(light_id)
            # If scene has raw hue/sat/ct, use those directly for exact restore
            if "hue" in config or "sat" in config or "ct" in config:
                raw_state: dict = {}
                raw_state["on"] = config.get("on", True)
                if config.get("brightness") is not None:
                    raw_state["bri"] = max(1, int(config["brightness"] * 254))
                colormode = config.get("colormode", "hs")
                if colormode == "ct" and "ct" in config:
                    raw_state["ct"] = config["ct"]
                else:
                    if "hue" in config:
                        raw_state["hue"] = config["hue"]
                    if "sat" in config:
                        raw_state["sat"] = config["sat"]
                light._put_state(raw_state)
            else:
                light.set(
                    color=config.get("color"),
                    brightness=config.get("brightness"),
                    on=config.get("on", True),
                )
        except KeyError:
            pass

    # Fork streaming for effect lights
    pid = None
    if effect_ids:
        pid = fork_stream(
            bridge.ip,
            bridge.api_key,
            bridge.client_key,
            scene,
        )

    return {"static": static_ids, "streaming": effect_ids, "pid": pid}


def stop_scene() -> bool:
    """Stop any running streaming scene."""
    from hue.stream import stop_stream

    return stop_stream()
