"""Spatial wave — a soft pulse drifts across the room, then it rests dim, repeat.

Position-aware: uses each light's real (x, y, z) coordinate so one broad pulse of
light travels through physical space along an axis, fully enters and exits, and
then the whole room rests at the dim `cool` color for a gap before the next.

The pulse is a raised-cosine bump (compact support, smoothly zero at its edges),
so it fades in and out with no pop. Make `width` comparable to the spacing
between your lights along the travel axis: if the bright region is narrower than
the gaps, brightness sags whenever the pulse sits between lights.

Timeline per cycle:
    [0 .. transit)        one pulse travels across the room (transit is derived:
                          the distance to cross at `speed`)
    [transit .. +rest)    whole room rests at `cool` (the dim gap)

Params (all optional):
    axis:    "x" | "y" | "z" — axis the pulse travels along (default "y").
    speed:   travel speed in normalized cube units per second (default 0.125).
    width:   half-width of the bright region in cube units (default 0.7; the
             full bright span is 2*width).
    rest:    seconds the room stays dim between pulses (default 8.0).
    reverse: travel the other direction (default False).
    hot:     [r, g, b] color at the pulse peak (default warm gold).
    cool:    [r, g, b] dim resting color between pulses (default deep indigo).
"""

import math


def render(
    t: float,
    x: float = 0.0,
    y: float = 0.0,
    z: float = 0.0,
    axis: str = "y",
    speed: float = 0.125,
    width: float = 0.7,
    rest: float = 8.0,
    reverse: bool = False,
    hot: tuple = (255, 140, 40),
    cool: tuple = (6, 8, 38),
    **params,
) -> tuple[float, float, float]:
    coord = {"x": x, "y": y, "z": z}.get(axis, y)

    # The pulse center travels from one width beyond -1 to one width beyond +1,
    # so it fully enters and exits the [-1, 1] span of light positions.
    span = 2.0 + 2.0 * width
    transit = span / speed
    period = transit + rest

    cycle = t % period
    i = 0.0
    if cycle < transit:
        p = cycle / transit  # 0..1 progress
        if reverse:
            p = 1.0 - p
        front = (-1.0 - width) + p * span
        d = coord - front
        if abs(d) < width:
            # Raised-cosine bump: 1 at the center, smoothly 0 at d = +/-width.
            i = 0.5 * (1.0 + math.cos(math.pi * d / width))

    r = cool[0] + (hot[0] - cool[0]) * i
    g = cool[1] + (hot[1] - cool[1]) * i
    b = cool[2] + (hot[2] - cool[2]) * i
    return (r, g, b)
