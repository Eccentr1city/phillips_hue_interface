"""Spatial sweep — a soft band of light that glides through the physical room.

A position-aware effect: it uses each light's real (x, y, z) coordinate (set on
the bridge from measured positions, see apply_positions.py) so a wavefront
travels across actual space. Lights brighten as the band reaches their location
and dim as it passes, so you see the light physically move through the room.

Params (all optional, set via scene/CLI):
    axis:  "x" | "y" | "z" — which room axis the band travels along (default "y",
           usually the long axis / depth).
    speed: how fast the band travels (default 0.4; a full there-and-back is
           roughly 2 / speed seconds).
    width: softness/size of the band (default 0.45, in normalized cube units).
    hot:   [r, g, b] color at the band's peak (default warm amber).
    cool:  [r, g, b] background color away from the band (default dim indigo).
"""

import math


def render(
    t: float,
    x: float = 0.0,
    y: float = 0.0,
    z: float = 0.0,
    axis: str = "y",
    speed: float = 0.4,
    width: float = 0.45,
    hot: tuple = (255, 110, 20),
    cool: tuple = (10, 6, 30),
    **params,
) -> tuple[int, int, int]:
    coord = {"x": x, "y": y, "z": z}.get(axis, y)

    # Smooth ping-pong wavefront sweeping between -1 and 1 and back.
    u = (t * speed) % 2.0
    triangle = 1.0 - abs(u - 1.0)  # 0 -> 1 -> 0 over the cycle
    front = triangle * 2.0 - 1.0  # -1 -> 1 -> -1

    # Gaussian falloff: brightest where the light sits on the wavefront.
    d = coord - front
    i = math.exp(-(d * d) / (2.0 * width * width))

    r = cool[0] + (hot[0] - cool[0]) * i
    g = cool[1] + (hot[1] - cool[1]) * i
    b = cool[2] + (hot[2] - cool[2]) * i

    return (
        min(255.0, max(0.0, r)),
        min(255.0, max(0.0, g)),
        min(255.0, max(0.0, b)),
    )
