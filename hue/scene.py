"""Scene definition, save/load, and apply logic."""

import json
from pathlib import Path

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
            Each config can have:
            - "color": color name or hex
            - "brightness": 0.0-1.0
            - "effect": effect name
            - "params": effect parameters dict

    Returns:
        Path to the saved scene file.
    """
    SCENES_DIR.mkdir(parents=True, exist_ok=True)
    data = {"name": name, "lights": lights}
    path = SCENES_DIR / f"{name}.json"
    path.write_text(json.dumps(data, indent=2) + "\n")
    return path


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

    # Apply static lights via REST
    for light_id_str, config in lights_config.items():
        light_id = int(light_id_str)
        if "effect" in config:
            effect_ids.append(light_id)
            continue

        # Static light
        static_ids.append(light_id)
        try:
            light = bridge.light(light_id)
            light.set(
                color=config.get("color"),
                brightness=config.get("brightness"),
                on=config.get("on", True),
            )
        except KeyError:
            pass  # Light not found on bridge, skip

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
