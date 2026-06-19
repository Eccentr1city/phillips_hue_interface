"""Subtle candle glow — a gentle, warm flame that breathes rather than roars.

Built off the built-in `candle` effect, but with a tight brightness band and
much softer flicker so it reads as a calm, ambient candlelight rather than an
active fire. Each light gets an independent phase offset (set automatically by
the streaming engine) so a group of lights looks like several candles, not one
synchronized blink.
"""

import math
import random

PHI = (1 + math.sqrt(5)) / 2

PARAMS = [
    {"name": "speed", "label": "Speed", "min": 0.2, "max": 3.0, "step": 0.05},
    {"name": "floor", "label": "Min brightness", "min": 0.0, "max": 0.9, "step": 0.02},
    {"name": "ceil", "label": "Max brightness", "min": 0.1, "max": 1.0, "step": 0.02},
]

# Per-light persistent state keyed by phase seed
_states: dict[float, dict] = {}


def _get_state(phase: float) -> dict:
    if phase not in _states:
        _states[phase] = {"crackle": 0.0, "smoothed_noise": 0.5}
    return _states[phase]


def render(
    t: float,
    speed: float = 1.0,
    phase: float = 0.0,
    floor: float = 0.45,
    ceil: float = 0.92,
) -> tuple[int, int, int]:
    """Render a soft candle flicker at time t (seconds).

    Args:
        t: Elapsed time in seconds.
        speed: Overall speed multiplier (default 1.0). Lower = lazier flame.
        phase: Per-light phase offset (set automatically by the engine).
        floor: Minimum normalized brightness (default 0.55) — keeps it from
               ever dropping to a dramatic low.
        ceil: Maximum normalized brightness (default 0.85) — keeps it gentle.

    Returns:
        (r, g, b) tuple, each 0-255.
    """
    ts = t * speed
    state = _get_state(phase)

    # Slow ember roll — the dominant, lazy movement. This is where most of the
    # visible variation lives, so it can swing fairly wide without looking busy.
    slow_roll = 0.20 * math.sin(ts * 0.08 * PHI + phase)

    # A second slow layer at a different period so the swing doesn't feel
    # metronomic — the two beat against each other for organic variation.
    slow_roll2 = 0.10 * math.sin(ts * 0.21 + phase * 1.7)

    # Mid waver — gentle sway.
    waver = 0.06 * math.sin(ts * 0.5 + phase * 2)

    # High-frequency flicker — kept small and slowed down so it reads as a
    # lazy sway rather than a busy jitter.
    flicker1 = 0.014 * abs(math.sin(ts * 3.1 + phase * 3))
    flicker2 = 0.009 * abs(math.sin(ts * 5.0 * PHI + phase * 5))

    # Rare, faint crackle — an occasional tiny lift, not a pop.
    if random.random() < 0.003:
        state["crackle"] = 0.012 + random.random() * 0.025
    state["crackle"] *= 0.9

    # Smoothed random noise, gently bounded — small steps = slow drift.
    state["smoothed_noise"] += (random.random() - 0.5) * 0.035
    state["smoothed_noise"] = max(0.4, min(0.6, state["smoothed_noise"]))
    noise = (state["smoothed_noise"] - 0.5) * 0.03

    # Center the flame around the middle of the band, then add the small layers.
    center = (floor + ceil) / 2
    intensity = (
        center
        + slow_roll
        + slow_roll2
        + waver
        + flicker1
        + flicker2
        + state["crackle"]
        + noise
    )
    intensity = max(floor, min(ceil, intensity))

    # Warm candle color: soft amber — more green than a roaring fire so it
    # leans warm-yellow instead of orange, with a hint of blue to desaturate.
    color_shift = (intensity - center) * 0.06
    r = 1.0 * intensity
    g = (0.55 + color_shift) * intensity
    b = 0.13 * intensity

    return (
        min(1.0, max(0.0, r)) * 255.0,
        min(1.0, max(0.0, g)) * 255.0,
        min(1.0, max(0.0, b)) * 255.0,
    )
