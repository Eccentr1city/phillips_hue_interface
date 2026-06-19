"""Spatial wave — a smooth band of light that travels continuously one way.

Position-aware: uses each light's real (x, y, z) coordinate so the wave moves
through physical space. Unlike `sweep` (which bounces back and forth), this is a
continuous traveling sinusoid — it glides in one direction forever with no
visible jump, because the wave is periodic in space.

Params (all optional):
    axis:       "x" | "y" | "z" — which room axis to travel along (default "y").
    speed:      cycles per second (default 0.08 — slow). Negative reverses
                direction.
    wavelength: spatial period in normalized cube units (default 2.0 ≈ one band
                spanning the room at a time).
    sharpness:  >1 tightens the bright crest and widens the dim troughs
                (default 1.6). 1.0 is a pure sinusoidal gradient.
    hot:        [r, g, b] color at the crest (default warm gold).
    cool:       [r, g, b] color in the troughs (default deep indigo).
"""

import math


def render(
    t: float,
    x: float = 0.0,
    y: float = 0.0,
    z: float = 0.0,
    axis: str = "y",
    speed: float = 0.08,
    wavelength: float = 2.0,
    sharpness: float = 1.6,
    hot: tuple = (255, 140, 40),
    cool: tuple = (6, 8, 38),
    **params,
) -> tuple[int, int, int]:
    coord = {"x": x, "y": y, "z": z}.get(axis, y)

    # Traveling sinusoid: continuous in time, periodic in space → no jump-back.
    s = math.sin(2.0 * math.pi * (coord / wavelength - t * speed))
    i = ((s + 1.0) / 2.0) ** sharpness  # 0..1, crest sharpened by `sharpness`

    r = cool[0] + (hot[0] - cool[0]) * i
    g = cool[1] + (hot[1] - cool[1]) * i
    b = cool[2] + (hot[2] - cool[2]) * i

    return (
        int(min(255, max(0, r))),
        int(min(255, max(0, g))),
        int(min(255, max(0, b))),
    )
