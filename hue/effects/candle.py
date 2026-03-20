"""Flickering candle effect — warm orange with random flicker."""

import math
import random


def render(t: float, speed: float = 1.0, warmth: float = 0.8) -> tuple[int, int, int]:
    """Render a candle flicker at time t (seconds).

    Args:
        t: Elapsed time in seconds.
        speed: Flicker speed multiplier (default 1.0).
        warmth: 0.0 = white-yellow, 1.0 = deep orange (default 0.8).

    Returns:
        (r, g, b) tuple, each 0-255.
    """
    ts = t * speed

    # Layered noise for organic flicker
    flicker = (
        0.5 * math.sin(ts * 7.3)
        + 0.3 * math.sin(ts * 13.1 + 1.7)
        + 0.2 * math.sin(ts * 23.7 + 3.1)
    )
    # Add some randomness
    flicker += random.gauss(0, 0.15)
    flicker = max(-1.0, min(1.0, flicker))

    # Base brightness with flicker
    brightness = 0.7 + 0.3 * flicker

    # Warm candle color
    r = int(255 * brightness)
    g = int((120 + 60 * (1 - warmth)) * brightness)
    b = int((30 + 40 * (1 - warmth)) * brightness)

    return (max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b)))
