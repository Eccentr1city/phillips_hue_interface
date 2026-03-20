"""Hob tool definitions — exports TOOL_DEFINITIONS and TOOL_FUNCTIONS.

Drop-in replacement for ~/hob/hue_tools.py. All functions are async.

Tools:
  hue_status  — lights + bridge info in one call
  hue_set     — the everything tool: lights, color, brightness, on/off, effects, scenes
  hue_stop    — stop streaming effects
  hue_list    — list available effects and saved scenes
  hue_define_effect — create a new effect .py file
  hue_define_scene  — create/update a scene (explicit config or snapshot current state)
"""

from pathlib import Path

TOOL_DEFINITIONS = [
    {
        "name": "hue_status",
        "description": (
            "Check what the lights are doing right now. "
            "Returns every light's name, ID, on/off, brightness, color, and reachability, "
            "plus bridge info. Use this before changing lights if you need to know current state."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "hue_set",
        "description": (
            "The main light control tool — handles everything in one call. Examples:\n"
            '  All lights red: {lights: "all", color: "red"}\n'
            "  Dim one light: {lights: 1, brightness: 0.3}\n"
            '  Turn off by name: {lights: "Cylinder lamp", on: false}\n'
            '  Fire effect: {lights: "all", effect: "candle"}\n'
            '  Apply a scene: {scene: "cozy"}\n'
            "Setting color or brightness implicitly turns the light on. "
            "Colors: red, orange, yellow, green, cyan, blue, purple, pink, "
            "white, warm white, cool white, or hex (#RRGGBB). "
            "Effects run as real-time DTLS streaming at 25fps. "
            "Use hue_list to see available effects and scenes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "lights": {
                    "description": (
                        "Which lights to control. "
                        'Light ID (int), name (string), "all", '
                        "or a list of IDs/names. Required unless using scene."
                    ),
                },
                "color": {
                    "type": "string",
                    "description": "Color name (e.g. 'red', 'warm white') or hex (#RRGGBB).",
                },
                "brightness": {
                    "type": "number",
                    "description": "Brightness 0.0 (dimmest) to 1.0 (full).",
                },
                "on": {
                    "type": "boolean",
                    "description": "Turn on (true) or off (false). Implied by color/brightness.",
                },
                "effect": {
                    "type": "string",
                    "description": (
                        "Start a streaming effect by name. "
                        "Built-in: 'candle' (realistic fire flicker), 'breathe' (slow color fade). "
                        "Stops any previously running effect."
                    ),
                },
                "effect_params": {
                    "type": "object",
                    "description": "Optional params for the effect (e.g. {speed: 0.5}).",
                },
                "scene": {
                    "type": "string",
                    "description": "Apply a saved scene by name. Ignores all other params.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "hue_stop",
        "description": (
            "Stop any running streaming effect (candle, breathe, etc.) and kill the "
            "background process. Lights will hold their last color. No-op if nothing is running."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "hue_list",
        "description": (
            "List all available effects (built-in + user-created) and saved scenes. "
            "Use this to check what's available before calling hue_set with effect or scene."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "hue_define_effect",
        "description": (
            "Create a new custom streaming effect. Write a Python file that defines "
            "render(t, **params) -> (r, g, b) where t is elapsed seconds and rgb are 0-255. "
            "The effect runs at 25fps via DTLS streaming. Each light gets a unique 'phase' "
            "param automatically so they vary independently. After creating, use it with "
            'hue_set: {lights: "all", effect: "your_effect_name"}'
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Effect name — becomes the filename and the name used in hue_set.",
                },
                "code": {
                    "type": "string",
                    "description": (
                        "Python source code. Must define: render(t, **params) -> (r, g, b). "
                        "Can use math, random, time from stdlib. "
                        "The 'phase' param differentiates lights."
                    ),
                },
            },
            "required": ["name", "code"],
        },
    },
    {
        "name": "hue_define_scene",
        "description": (
            "Save a lighting scene for quick recall later. Two modes:\n"
            "  Snapshot current: {name: 'cozy', from_current: true} — saves whatever "
            "the lights are doing right now.\n"
            '  Explicit config: {name: "movie", lights: {"1": {color: "blue", brightness: 0.3}, '
            '"2": {color: "purple"}}} — define exactly what each light should do.\n'
            "Light configs can include: color, brightness (0-1), effect (name), params. "
            "Apply saved scenes with hue_set: {scene: 'cozy'}"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Scene name for saving and recalling.",
                },
                "lights": {
                    "type": "object",
                    "description": (
                        "Mapping of light ID (string) to config object. "
                        "Each config: {color, brightness, effect, params}. "
                        "Not needed if from_current is true."
                    ),
                },
                "from_current": {
                    "type": "boolean",
                    "description": "Snapshot the current state of all lights as the scene.",
                },
            },
            "required": ["name"],
        },
    },
]


def _get_bridge():
    from hue.bridge import Bridge

    return Bridge()


async def hue_status(**kwargs) -> str:
    bridge = _get_bridge()

    # Bridge info
    cfg = bridge.info()
    info_lines = [
        f"Bridge: {cfg.get('name', '?')} ({cfg.get('modelid', '?')})",
        f"  IP: {bridge.ip} | API: {cfg.get('apiversion', '?')} | SW: {cfg.get('swversion', '?')}",
        "",
    ]

    # Lights
    light_lines = []
    for lid, light in sorted(bridge.lights.items()):
        state = light.state
        on_off = "ON" if state.get("on") else "OFF"
        reachable = "" if state.get("reachable") else " UNREACHABLE"
        bri = state.get("bri")
        bri_str = f" bri={round(bri / 254, 2)}" if bri is not None else ""
        hue_val = state.get("hue")
        sat_val = state.get("sat")
        ct_val = state.get("ct")
        color_parts = []
        if hue_val is not None:
            color_parts.append(f"hue={hue_val}")
        if sat_val is not None:
            color_parts.append(f"sat={sat_val}")
        if ct_val is not None:
            color_parts.append(f"ct={ct_val}")
        color_str = f" ({', '.join(color_parts)})" if color_parts else ""
        light_lines.append(
            f"  [{lid}] {light.name}: {on_off}{bri_str}{color_str}{reachable}"
        )

    return "\n".join(info_lines + light_lines)


async def hue_set(
    lights=None,
    color=None,
    brightness=None,
    on=None,
    effect=None,
    effect_params=None,
    scene=None,
    **kwargs,
) -> str:
    bridge = _get_bridge()

    # Scene mode — apply a saved scene and return
    if scene is not None:
        from hue.scene import apply_scene

        result = apply_scene(bridge, scene)
        parts = []
        if result["static"]:
            parts.append(f"{len(result['static'])} static")
        if result["streaming"]:
            parts.append(f"{len(result['streaming'])} streaming")
        return f"Scene '{scene}' applied: {', '.join(parts) or 'empty'}."

    if lights is None:
        return "Error: specify 'lights' (ID, name, list, or 'all') or 'scene'."

    resolved = bridge.resolve_lights(lights)

    # Effect mode — start streaming effects on the target lights
    if effect is not None:
        from hue.effects import get_effect
        from hue.stream import start_stream

        get_effect(effect)  # validate it exists

        # Build a scene-like dict for the streaming daemon
        scene_data = {
            "lights": {
                str(light.id): {
                    "effect": effect,
                    "params": effect_params or {},
                }
                for light in resolved
            }
        }
        pid = start_stream(bridge.ip, bridge.api_key, bridge.client_key, scene_data)
        names = ", ".join(light.name for light in resolved)
        return f"Effect '{effect}' started on {names} (pid={pid})."

    # Static mode — set color/brightness/on/off
    names = []
    for light in resolved:
        light.set(color=color, brightness=brightness, on=on)
        names.append(light.name)

    parts = []
    if on is not None:
        parts.append("on" if on else "off")
    if color:
        parts.append(f"color={color}")
    if brightness is not None:
        parts.append(f"brightness={brightness}")
    return f"Set {', '.join(names)}: {', '.join(parts) or 'updated'}."


async def hue_stop(**kwargs) -> str:
    from hue.scene import stop_scene

    if stop_scene():
        return "Streaming stopped."
    return "No streaming process was running."


async def hue_list(**kwargs) -> str:
    from hue.effects import list_effects
    from hue.scene import list_scenes

    lines = ["Effects:"]
    effects = list_effects()
    if effects:
        for eff in effects:
            source = "built-in" if eff["builtin"] else "user"
            desc = f" -- {eff['description']}" if eff["description"] else ""
            lines.append(f"  {eff['name']} ({source}){desc}")
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append("Scenes:")
    scenes = list_scenes()
    if scenes:
        for scene in scenes:
            count = len(scene["lights"])
            lines.append(f"  {scene['name']} ({count} light(s))")
    else:
        lines.append("  (none)")

    return "\n".join(lines)


async def hue_define_effect(name: str, code: str, **kwargs) -> str:
    effects_dir = Path(__file__).resolve().parent.parent / "effects"
    effects_dir.mkdir(parents=True, exist_ok=True)
    path = effects_dir / f"{name}.py"
    path.write_text(code)

    from hue.effects import get_effect

    try:
        get_effect(name)
    except Exception as exc:
        path.unlink()
        return f"Error: effect code is invalid -- {exc}"

    return f"Effect '{name}' saved to {path}."


async def hue_define_scene(
    name: str, lights: dict | None = None, from_current: bool = False, **kwargs
) -> str:
    if from_current:
        from hue.scene import save_scene_from_current

        bridge = _get_bridge()
        path = save_scene_from_current(bridge, name)
        return f"Scene '{name}' saved from current state to {path}."

    if lights is None:
        return "Error: provide 'lights' config or set from_current=true."

    from hue.scene import save_scene

    path = save_scene(name, lights)
    return f"Scene '{name}' saved to {path}."


TOOL_FUNCTIONS = {
    "hue_status": hue_status,
    "hue_set": hue_set,
    "hue_stop": hue_stop,
    "hue_list": hue_list,
    "hue_define_effect": hue_define_effect,
    "hue_define_scene": hue_define_scene,
}
