"""Spatial wave — a single pulse sweeps the room, then it rests dim, and repeats.

Position-aware: uses each light's real (x, y, z) coordinate so one soft pulse of
light travels through physical space along an axis, fully enters and exits, and
then the whole room sits at the dim `cool` color for a gap before the next pulse.

Timeline per cycle (length `period`):
    [0 .. transit)        one pulse travels across the room
    [transit .. period)   whole room rests at `cool` (the dim gap)

Params (all optional):
    axis:    "x" | "y" | "z" — axis the pulse travels along (default "y").
    period:  full cycle length in seconds, pulse + dark gap (default 9.0).
    transit: seconds for the pulse to cross the room (default 3.0). The dark gap
             is period - transit (so ~6s dim by default).
    width:   pulse size in normalized cube units (default 0.3).
    reverse: travel the other direction (default False).
    hot:     [r, g, b] color at the pulse peak (default warm gold).
    cool:    [r, g, b] dim resting color between pulses (default low warm).
"""

import math


def render(
    t: float,
    x: float = 0.0,
    y: float = 0.0,
    z: float = 0.0,
    axis: str = "y",
    period: float = 9.0,
    transit: float = 3.0,
    width: float = 0.3,
    reverse: bool = False,
    hot: tuple = (255, 150, 50),
    cool: tuple = (16, 7, 2),
    **params,
) -> tuple[float, float, float]:
    coord = {"x": x, "y": y, "z": z}.get(axis, y)

    cycle = t % period
    if cycle < transit:
        p = cycle / transit  # 0..1 progress across the room
        if reverse:
            p = 1.0 - p
        # Start/end the pulse center well outside the cube (margin = 3*width) so
        # it fades fully in and out — no visible pop at the edges of the sweep.
        margin = 3.0 * width
        front = (-1.0 - margin) + p * (2.0 + 2.0 * margin)
        d = (coord - front) / width
        i = math.exp(-0.5 * d * d)
    else:
        i = 0.0  # dim rest between pulses

    r = cool[0] + (hot[0] - cool[0]) * i
    g = cool[1] + (hot[1] - cool[1]) * i
    b = cool[2] + (hot[2] - cool[2]) * i
    return (r, g, b)
