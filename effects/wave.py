"""Spatial wave — a soft pulse drifts across the room, then it rests dim, repeat.

Position-aware: uses each light's real (x, y, z) coordinate so one broad pulse of
light travels through physical space, fully enters and exits, and then the whole
room rests at the dim `cool` color for a gap before the next.

The travel direction lives in the x-y (floor) plane and is set by `angle`:
0 deg travels along +Y, 90 deg along +X, so e.g. 20 deg is mostly along Y but
tilted toward X. Because positions were normalized with a single uniform scale,
this angle matches the real physical angle in the room.

The pulse is a raised-cosine bump (compact support, smoothly zero at its edges),
so it fades in and out with no pop. Make `width` comparable to the spacing
between your lights along the travel direction: if the bright region is narrower
than the gaps, brightness sags whenever the pulse sits between lights.

Params (all optional):
    angle:   travel direction in degrees, measured off +Y toward +X (default 20).
    speed:   travel speed in normalized cube units per second (default 0.16).
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
    angle: float = 20.0,
    speed: float = 0.16,
    width: float = 0.7,
    rest: float = 8.0,
    reverse: bool = False,
    hot: tuple = (255, 140, 40),
    cool: tuple = (6, 8, 38),
    **params,
) -> tuple[float, float, float]:
    # Signed distance of this light along the (tilted) travel direction.
    a = math.radians(angle)
    dir_x, dir_y = math.sin(a), math.cos(a)
    coord = x * dir_x + y * dir_y

    # Largest |coord| any point in the unit floor-cube can have for this
    # direction — so the pulse fully covers every light as it crosses.
    bound = abs(dir_x) + abs(dir_y)
    span = 2.0 * bound + 2.0 * width
    transit = span / speed
    period = transit + rest

    cycle = t % period
    i = 0.0
    if cycle < transit:
        p = cycle / transit  # 0..1 progress
        if reverse:
            p = 1.0 - p
        front = -(bound + width) + p * span
        d = coord - front
        if abs(d) < width:
            # Raised-cosine bump: 1 at the center, smoothly 0 at d = +/-width.
            i = 0.5 * (1.0 + math.cos(math.pi * d / width))

    r = cool[0] + (hot[0] - cool[0]) * i
    g = cool[1] + (hot[1] - cool[1]) * i
    b = cool[2] + (hot[2] - cool[2]) * i
    return (r, g, b)
