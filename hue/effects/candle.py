"""Realistic fire/candle effect — layered flicker with ember glow and crackle.

Ported from fire_entertainment.js. Each light gets independent flicker based on
a golden-ratio phase offset, so multiple lights look like different parts of the
same fire rather than flickering in sync.
"""

import math
import random

PHI = (1 + math.sqrt(5)) / 2

# Per-call persistent state keyed by phase seed
_states: dict[float, dict] = {}


def _get_state(phase: float) -> dict:
    if phase not in _states:
        _states[phase] = {"crackle": 0.0, "smoothed_noise": 0.5}
    return _states[phase]


def render(t: float, speed: float = 1.0, phase: float = 0.0) -> tuple[int, int, int]:
    """Render a fire flicker at time t (seconds).

    Args:
        t: Elapsed time in seconds.
        speed: Overall speed multiplier (default 1.0).
        phase: Phase offset for this light — use different values per light
               so they flicker independently. The streaming engine sets this
               automatically based on channel ID.

    Returns:
        (r, g, b) tuple, each 0-255.
    """
    ts = t * speed
    state = _get_state(phase)

    # Low frequency: slow rolling ember glow
    slow_roll = 0.5 + 0.15 * math.sin(ts * 0.08 * PHI + phase)

    # Mid frequency: gentle wavering
    waver = 0.1 * math.sin(ts * 0.5 + phase * 2)

    # High frequency: quick flickers (asymmetric spikes)
    flicker1 = 0.07 * abs(math.sin(ts * 4.7 + phase * 3))
    flicker2 = 0.05 * abs(math.sin(ts * 7.3 * PHI + phase * 5))
    flicker3 = 0.03 * abs(math.sin(ts * 11.1 + phase * 7))

    # Random crackle
    if random.random() < 0.012:
        state["crackle"] = 0.1 + random.random() * 0.15
    state["crackle"] *= 0.85

    # Smoothed noise
    state["smoothed_noise"] += (random.random() - 0.5) * 0.1
    state["smoothed_noise"] = max(0.3, min(0.7, state["smoothed_noise"]))
    noise = (state["smoothed_noise"] - 0.5) * 0.1

    intensity = (
        slow_roll + waver + flicker1 + flicker2 + flicker3 + state["crackle"] + noise
    )
    intensity = max(0.15, min(1.0, intensity))

    # Fire color: warm orange base, brightness-driven with subtle color shift
    brightness = 0.25 + intensity * 0.75
    color_shift = (intensity - 0.5) * 0.08

    r = 1.0 * brightness
    g = (0.38 + color_shift) * brightness
    b = 0.03 * brightness

    return (
        int(min(1, max(0, r)) * 255),
        int(min(1, max(0, g)) * 255),
        int(min(1, max(0, b)) * 255),
    )
