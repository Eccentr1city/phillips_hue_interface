"""CLI entry point for the hue command."""

import sys


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        _print_help()
        return

    cmd = args[0]
    if cmd == "status":
        _cmd_status()
    elif cmd == "set":
        _cmd_set(args[1:])
    elif cmd == "off":
        _cmd_off(args[1:])
    elif cmd == "on":
        _cmd_on(args[1:])
    elif cmd == "list":
        _cmd_list()
    elif cmd == "scene":
        _cmd_scene(args[1:])
    elif cmd == "stop":
        _cmd_stop()
    elif cmd == "beatsync":
        _cmd_beatsync(args[1:])
    elif cmd == "ui":
        _cmd_ui(args[1:])
    else:
        print(f"Unknown command: {cmd}")
        _print_help()
        sys.exit(1)


def _print_help():
    print("Usage: hue <command> [args]")
    print()
    print("Commands:")
    print("  status                          Show all lights and bridge info")
    print(
        "  set <lights> --color <color>    Set color (and optionally --brightness 0-1)"
    )
    print("  set <lights> --effect <name>    Start an effect (mode per effect)")
    print(
        "       [--smooth | --stream]      Force smooth (REST firmware fades) or "
        "streaming. Smooth takes --interval/--transition; effect params (e.g. "
        "--speed, --width) pass through as flags."
    )
    print("  on [lights]                     Turn on (default: all)")
    print("  off [lights]                    Turn off (default: all)")
    print("  list                            List available effects and scenes")
    print("  scene set <name>                Apply a saved scene")
    print("  scene save <name>               Save current state as a scene")
    print("  stop                            Stop streaming effects")
    print(
        "  beatsync [--list] [--device <n>]    Sync lights to live audio "
        "(loopback capture; --sensitivity/--decay/--flavor tunable)"
    )
    print("  ui [--port 8765]                Web control panel with live sliders")
    print()
    print("Lights: ID number, light name, 'all', or comma-separated list")


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


def _parse_lights_target(arg: str) -> int | str | list:
    """Parse a lights argument: int, name, 'all', or comma-separated list."""
    if arg.lower() == "all":
        return "all"
    # Comma-separated?
    if "," in arg:
        parts = []
        for part in arg.split(","):
            part = part.strip()
            try:
                parts.append(int(part))
            except ValueError:
                parts.append(part)
        return parts
    try:
        return int(arg)
    except ValueError:
        return arg


def _cmd_status():
    import asyncio

    from hue.tools import hue_status

    print(asyncio.run(hue_status()))


def _cmd_set(args: list[str]):
    if not args:
        print("Usage: hue set <lights> --color <color> [--brightness <0-1>]")
        sys.exit(1)

    # Bare boolean flags forcing a rendering mode; pull them out first.
    force_smooth = "--smooth" in args
    force_stream = "--stream" in args
    args = [a for a in args if a not in ("--smooth", "--stream")]

    target = _parse_lights_target(args[0])
    flags = _parse_flags(args[1:])
    bridge = _get_bridge()

    resolved = bridge.resolve_lights(target)
    effect_name = flags.get("effect")

    if effect_name:
        from hue.effects import get_effect

        eff = get_effect(effect_name)  # validate

        # Mode: explicit flag wins, else the effect's declared MODE, else stream.
        if force_smooth:
            mode = "smooth"
        elif force_stream:
            mode = "streaming"
        else:
            mode = eff.get("mode") or "streaming"

        # Any flags other than the reserved ones are passed to the effect as
        # params (e.g. --speed 0.3 --width 0.5), numbers when parseable.
        reserved = {"effect", "interval", "transition", "color", "brightness"}
        params = {}
        for key, value in flags.items():
            if key in reserved:
                continue
            try:
                params[key] = float(value)
            except ValueError:
                params[key] = value

        scene_data = {
            "lights": {
                str(lt.id): {"effect": effect_name, "params": params} for lt in resolved
            }
        }
        names = ", ".join(lt.name for lt in resolved)

        if mode == "smooth":
            # Smooth (REST + firmware fades) — for slow ambient effects.
            from hue.smooth import start_smooth
            from hue.stream import stop_stream

            stop_stream()  # don't let both modes fight over the lights
            interval = float(flags.get("interval", 0.4))
            transition = float(flags.get("transition", 1.0))
            pid = start_smooth(
                bridge.ip,
                bridge.api_key,
                bridge.client_key,
                scene_data,
                interval,
                transition,
            )
            print(
                f"Effect '{effect_name}' started (smooth mode) on {names} (pid={pid})"
            )
        else:
            # Streaming (entertainment / DTLS) — for fast effects.
            from hue.smooth import stop_smooth
            from hue.stream import start_stream

            stop_smooth()
            pid = start_stream(bridge.ip, bridge.api_key, bridge.client_key, scene_data)
            print(f"Effect '{effect_name}' started on {names} (pid={pid})")
    else:
        color = flags.get("color")
        brightness = float(flags["brightness"]) if "brightness" in flags else None
        for light in resolved:
            light.set(color=color, brightness=brightness)
        names = ", ".join(lt.name for lt in resolved)
        parts = []
        if color:
            parts.append(f"color={color}")
        if brightness is not None:
            parts.append(f"brightness={brightness}")
        print(f"Set {names}: {', '.join(parts)}")


def _cmd_on(args: list[str]):
    bridge = _get_bridge()
    target = _parse_lights_target(args[0]) if args else "all"
    for light in bridge.resolve_lights(target):
        light.on()
        print(f"  {light.name} on")


def _cmd_off(args: list[str]):
    bridge = _get_bridge()
    target = _parse_lights_target(args[0]) if args else "all"
    for light in bridge.resolve_lights(target):
        light.off()
        print(f"  {light.name} off")


def _cmd_list():
    import asyncio

    from hue.tools import hue_list

    print(asyncio.run(hue_list()))


def _cmd_scene(args: list[str]):
    if not args:
        print("Usage: hue scene <set|save> <name>")
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

    elif action == "save":
        if len(args) < 2:
            print("Usage: hue scene save <name>")
            sys.exit(1)
        name = args[1]
        bridge = _get_bridge()
        from hue.scene import save_scene_from_current

        path = save_scene_from_current(bridge, name)
        print(f"Scene '{name}' saved from current state to {path}")

    else:
        print(f"Unknown scene action: {action}")
        sys.exit(1)


def _cmd_beatsync(args: list[str]):
    from hue.audiosync import list_devices, run

    if "--list" in args:
        list_devices()
        return
    flags = _parse_flags(args)
    run(
        device=flags.get("device"),
        color=flags.get("color", "red"),
        flavor_color=flags.get("flavor-color", "amber"),
        flavor=flags.get("flavor"),
        sensitivity=float(flags.get("sensitivity", 1.7)),
        refractory=float(flags.get("refractory", 0.34)),
        decay=float(flags.get("decay", 0.35)),
        attack=float(flags.get("attack", 0.10)),
        min_bright=float(flags.get("min-bright", 0.04)),
        max_bright=float(flags.get("max-bright", 0.22)),
        flavor_max=float(flags.get("flavor-max", 0.10)),
        gain=float(flags.get("gain", 0.0)),
        floor=float(flags.get("floor", 0.04)),
    )


def _cmd_ui(args: list[str]):
    from hue.webui import serve

    flags = _parse_flags(args)
    serve(port=int(flags.get("port", 8765)))


def _cmd_stop():
    from hue.scene import stop_scene
    from hue.smooth import stop_smooth

    stopped = stop_scene()  # streaming daemon
    stopped = stop_smooth() or stopped  # smooth daemon
    print("Stopped." if stopped else "No effect running.")
