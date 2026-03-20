"""Slow color breathing effect — smoothly cycles through colors."""

import math


def render(
    t: float,
    speed: float = 0.3,
    r1: int = 255,
    g1: int = 50,
    b1: int = 50,
    r2: int = 50,
    g2: int = 50,
    b2: int = 255,
) -> tuple[int, int, int]:
    """Render a breathing color cycle at time t (seconds).

    Smoothly interpolates between two colors using a sine wave.

    Args:
        t: Elapsed time in seconds.
        speed: Cycle speed (default 0.3 = ~3.3s per full cycle).
        r1, g1, b1: First color (default warm red).
        r2, g2, b2: Second color (default blue).

    Returns:
        (r, g, b) tuple, each 0-255.
    """
    # Sine wave 0..1
    mix = (math.sin(t * speed * 2 * math.pi) + 1) / 2

    r = int(r1 + (r2 - r1) * mix)
    g = int(g1 + (g2 - g1) * mix)
    b = int(b1 + (b2 - b1) * mix)

    return (max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b)))
