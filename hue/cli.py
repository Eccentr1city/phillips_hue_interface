"""CLI entry point for the hue command."""

import sys


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        _print_help()
        return

    cmd = args[0]
    if cmd == "light":
        _cmd_light(args[1:])
    elif cmd == "all":
        _cmd_all(args[1:])
    elif cmd == "effects":
        _cmd_effects(args[1:])
    elif cmd == "scenes":
        _cmd_scenes(args[1:])
    elif cmd == "scene":
        _cmd_scene(args[1:])
    else:
        print(f"Unknown command: {cmd}")
        _print_help()
        sys.exit(1)


def _print_help():
    print("Usage: hue <command> [args]")
    print()
    print("Commands:")
    print("  light <id|name> set --color <color> [--brightness <0-1>]")
    print("  light <id|name> on")
    print("  light <id|name> off")
    print("  all set --color <color> [--brightness <0-1>]")
    print("  all on")
    print("  all off")
    print("  effects list")
    print("  scenes list")
    print("  scene set <name>")
    print("  scene stop")


def _get_bridge():
    from hue.bridge import Bridge

    return Bridge()


def _parse_flags(args: list[str]) -> dict[str, str]:
    """Parse --key value pairs from args."""
    flags = {}
    i = 0
    while i < len(args):
        if args[i].startswith("--") and i + 1 < len(args):
            flags[args[i][2:]] = args[i + 1]
            i += 2
        else:
            i += 1
    return flags


def _cmd_light(args: list[str]):
    if len(args) < 2:
        print(
            "Usage: hue light <id|name> <set|on|off> [--color <color>] [--brightness <0-1>]"
        )
        sys.exit(1)

    target = args[0]
    action = args[1]
    bridge = _get_bridge()

    # Try to parse as int (light ID), otherwise use as name
    try:
        light = bridge.light(int(target))
    except ValueError:
        light = bridge.light(target)

    if action == "on":
        light.on()
        print(f"Light {light.name} turned on")
    elif action == "off":
        light.off()
        print(f"Light {light.name} turned off")
    elif action == "set":
        flags = _parse_flags(args[2:])
        color = flags.get("color")
        brightness = float(flags["brightness"]) if "brightness" in flags else None
        light.set(color=color, brightness=brightness)
        parts = []
        if color:
            parts.append(f"color={color}")
        if brightness is not None:
            parts.append(f"brightness={brightness}")
        print(f"Light {light.name} set: {', '.join(parts)}")
    else:
        print(f"Unknown action: {action}")
        sys.exit(1)


def _cmd_all(args: list[str]):
    if not args:
        print("Usage: hue all <set|on|off> [--color <color>] [--brightness <0-1>]")
        sys.exit(1)

    action = args[0]
    bridge = _get_bridge()

    if action == "on":
        bridge.all.on()
        print("All lights turned on")
    elif action == "off":
        bridge.all.off()
        print("All lights turned off")
    elif action == "set":
        flags = _parse_flags(args[1:])
        color = flags.get("color")
        brightness = float(flags["brightness"]) if "brightness" in flags else None
        bridge.all.set(color=color, brightness=brightness)
        parts = []
        if color:
            parts.append(f"color={color}")
        if brightness is not None:
            parts.append(f"brightness={brightness}")
        print(f"All lights set: {', '.join(parts)}")
    else:
        print(f"Unknown action: {action}")
        sys.exit(1)


def _cmd_effects(args: list[str]):
    if not args or args[0] != "list":
        print("Usage: hue effects list")
        sys.exit(1)

    from hue.effects import list_effects

    effects = list_effects()
    if not effects:
        print("No effects found.")
        return

    for eff in effects:
        source = "built-in" if eff["builtin"] else "user"
        desc = f" — {eff['description']}" if eff["description"] else ""
        print(f"  {eff['name']} ({source}){desc}")


def _cmd_scenes(args: list[str]):
    if not args or args[0] != "list":
        print("Usage: hue scenes list")
        sys.exit(1)

    from hue.scene import list_scenes

    scenes = list_scenes()
    if not scenes:
        print("No scenes found.")
        return

    for scene in scenes:
        light_count = len(scene["lights"])
        print(f"  {scene['name']} ({light_count} light(s))")


def _cmd_scene(args: list[str]):
    if not args:
        print("Usage: hue scene <set|stop> [name]")
        sys.exit(1)

    action = args[0]

    if action == "set":
        if len(args) < 2:
            print("Usage: hue scene set <name>")
            sys.exit(1)
        name = args[1]
        bridge = _get_bridge()
        from hue.scene import apply_scene

        result = apply_scene(bridge, name)
        static = result["static"]
        streaming = result["streaming"]
        parts = []
        if static:
            parts.append(f"{len(static)} static")
        if streaming:
            parts.append(f"{len(streaming)} streaming (pid={result['pid']})")
        print(f"Scene '{name}' applied: {', '.join(parts) or 'empty'}")

    elif action == "stop":
        from hue.scene import stop_scene

        if stop_scene():
            print("Streaming stopped.")
        else:
            print("No streaming process running.")
    else:
        print(f"Unknown scene action: {action}")
        sys.exit(1)
