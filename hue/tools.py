"""Hob tool definitions — exports TOOL_DEFINITIONS and TOOL_FUNCTIONS.

Drop-in replacement for ~/hob/hue_tools.py. All functions are async.
"""

from pathlib import Path

TOOL_DEFINITIONS = [
    {
        "name": "hue_list_lights",
        "description": "List all Hue lights with their current state (on/off, color, brightness).",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "hue_set_light",
        "description": "Set a Hue light to a static color, brightness, or on/off state.",
        "input_schema": {
            "type": "object",
            "properties": {
                "light": {
                    "type": ["integer", "string"],
                    "description": "Light ID (integer) or name (string).",
                },
                "color": {
                    "type": "string",
                    "description": "Color name (red, blue, warm white, etc.) or hex (#RRGGBB). Optional.",
                },
                "brightness": {
                    "type": "number",
                    "description": "Brightness from 0.0 to 1.0. Optional.",
                },
                "on": {
                    "type": "boolean",
                    "description": "Turn on (true) or off (false). Optional.",
                },
            },
            "required": ["light"],
        },
    },
    {
        "name": "hue_list_effects",
        "description": "List all available lighting effects (built-in and user-defined).",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "hue_define_effect",
        "description": "Create a new lighting effect by writing a Python file with a render(t, **params) -> (r, g, b) function.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Effect name (becomes the filename, e.g. 'campfire').",
                },
                "code": {
                    "type": "string",
                    "description": "Python source code. Must define render(t, **params) -> (r, g, b) where t is elapsed seconds and rgb are 0-255.",
                },
            },
            "required": ["name", "code"],
        },
    },
    {
        "name": "hue_list_scenes",
        "description": "List all saved lighting scenes.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "hue_define_scene",
        "description": "Create or update a scene — a named configuration mapping lights to colors or effects.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Scene name (e.g. 'cozy', 'movie').",
                },
                "lights": {
                    "type": "object",
                    "description": "Mapping of light ID (string) to config. Each config can have: color (string), brightness (number 0-1), effect (string effect name), params (object of effect parameters).",
                    "additionalProperties": {
                        "type": "object",
                        "properties": {
                            "color": {"type": "string"},
                            "brightness": {"type": "number"},
                            "effect": {"type": "string"},
                            "params": {"type": "object"},
                        },
                    },
                },
            },
            "required": ["name", "lights"],
        },
    },
    {
        "name": "hue_set_scene",
        "description": "Apply a saved scene. Static lights are set via REST; lights with effects start a background streaming process.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Name of the scene to apply.",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "hue_stop_scene",
        "description": "Stop any running streaming effect and kill the background process.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]


def _get_bridge():
    from hue.bridge import Bridge

    return Bridge()


async def hue_list_lights(**kwargs) -> str:
    bridge = _get_bridge()
    lights = bridge.lights
    result = []
    for lid, light in sorted(lights.items()):
        state = light.state
        result.append(
            {
                "id": lid,
                "name": light.name,
                "on": state.get("on", False),
                "brightness": round(state.get("bri", 0) / 254, 2),
                "reachable": state.get("reachable", False),
            }
        )
    import json

    return json.dumps(result, indent=2)


async def hue_set_light(light, color=None, brightness=None, on=None, **kwargs) -> str:
    bridge = _get_bridge()
    target = bridge.light(light)
    target.set(color=color, brightness=brightness, on=on)
    return f"Light '{target.name}' updated."


async def hue_list_effects(**kwargs) -> str:
    from hue.effects import list_effects
    import json

    effects = list_effects()
    return json.dumps(
        [
            {
                "name": e["name"],
                "builtin": e["builtin"],
                "description": e["description"],
            }
            for e in effects
        ],
        indent=2,
    )


async def hue_define_effect(name: str, code: str, **kwargs) -> str:
    effects_dir = Path(__file__).resolve().parent.parent / "effects"
    effects_dir.mkdir(parents=True, exist_ok=True)
    path = effects_dir / f"{name}.py"
    path.write_text(code)

    # Verify it loads
    from hue.effects import get_effect

    try:
        get_effect(name)
    except Exception as e:
        path.unlink()
        return f"Error: effect code is invalid — {e}"

    return f"Effect '{name}' saved to {path}."


async def hue_list_scenes(**kwargs) -> str:
    from hue.scene import list_scenes
    import json

    scenes = list_scenes()
    return json.dumps(
        [{"name": s["name"], "lights": s["lights"]} for s in scenes],
        indent=2,
    )


async def hue_define_scene(name: str, lights: dict, **kwargs) -> str:
    from hue.scene import save_scene

    path = save_scene(name, lights)
    return f"Scene '{name}' saved to {path}."


async def hue_set_scene(name: str, **kwargs) -> str:
    from hue.scene import apply_scene

    bridge = _get_bridge()
    result = apply_scene(bridge, name)
    parts = []
    if result["static"]:
        parts.append(f"{len(result['static'])} static light(s)")
    if result["streaming"]:
        parts.append(f"{len(result['streaming'])} streaming light(s)")
    return f"Scene '{name}' applied: {', '.join(parts) or 'empty scene'}."


async def hue_stop_scene(**kwargs) -> str:
    from hue.scene import stop_scene

    if stop_scene():
        return "Streaming stopped."
    return "No streaming process was running."


TOOL_FUNCTIONS = {
    "hue_list_lights": hue_list_lights,
    "hue_set_light": hue_set_light,
    "hue_list_effects": hue_list_effects,
    "hue_define_effect": hue_define_effect,
    "hue_list_scenes": hue_list_scenes,
    "hue_define_scene": hue_define_scene,
    "hue_set_scene": hue_set_scene,
    "hue_stop_scene": hue_stop_scene,
}
