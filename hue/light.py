"""Light model with .set(), .on(), .off()."""

from __future__ import annotations

from typing import TYPE_CHECKING

from hue.bridge import COLOR_MAP

if TYPE_CHECKING:
    from hue.bridge import Bridge


class Light:
    """Represents a single Hue light."""

    def __init__(self, bridge: Bridge, id: int, name: str, raw: dict):
        self.bridge = bridge
        self.id = id
        self.name = name
        self.raw = raw

    def _put_state(self, data: dict) -> list:
        return self.bridge._put(f"/lights/{self.id}/state", data)

    def set(
        self,
        color: str | None = None,
        brightness: float | None = None,
        on: bool | None = None,
    ):
        """Set light state.

        Args:
            color: Color name (e.g. "red", "warm white") or hex string ("#FF0000").
            brightness: 0.0 to 1.0.
            on: True/False to turn on/off.
        """
        data: dict = {}

        if on is not None:
            data["on"] = on
        elif color is not None or brightness is not None:
            # Implicitly turn on when setting color/brightness
            data["on"] = True

        if color is not None:
            color_lower = color.lower().strip()
            if color_lower in COLOR_MAP:
                hue, sat = COLOR_MAP[color_lower]
                data["hue"] = hue
                data["sat"] = sat
            elif color_lower.startswith("#") and len(color_lower) == 7:
                # Convert hex to xy (approximate via hue/sat)
                r = int(color_lower[1:3], 16)
                g = int(color_lower[3:5], 16)
                b = int(color_lower[5:7], 16)
                data.update(_rgb_to_hue_sat(r, g, b))
            else:
                raise ValueError(
                    f"Unknown color '{color}'. Use a name ({', '.join(COLOR_MAP)}) or hex (#RRGGBB)."
                )

        if brightness is not None:
            # Clamp to 0.0-1.0, map to 1-254
            bri = max(0.0, min(1.0, brightness))
            data["bri"] = max(1, int(bri * 254))

        if data:
            self._put_state(data)

    def on(self):
        self._put_state({"on": True})

    def off(self):
        self._put_state({"on": False})

    @property
    def state(self) -> dict:
        """Fetch current state from the bridge."""
        info = self.bridge._get(f"/lights/{self.id}")
        self.raw = info
        return info.get("state", {})

    def __repr__(self):
        return f"Light({self.id}, name={self.name!r})"


def _rgb_to_hue_sat(r: int, g: int, b: int) -> dict:
    """Convert RGB (0-255) to Hue API hue/sat values (approximate)."""
    r_norm, g_norm, b_norm = r / 255, g / 255, b / 255
    max_c = max(r_norm, g_norm, b_norm)
    min_c = min(r_norm, g_norm, b_norm)
    delta = max_c - min_c

    # Hue calculation
    if delta == 0:
        h = 0.0
    elif max_c == r_norm:
        h = 60 * (((g_norm - b_norm) / delta) % 6)
    elif max_c == g_norm:
        h = 60 * ((b_norm - r_norm) / delta + 2)
    else:
        h = 60 * ((r_norm - g_norm) / delta + 4)

    # Saturation
    s = 0.0 if max_c == 0 else delta / max_c

    hue_val = int((h / 360) * 65535) % 65536
    sat_val = int(s * 254)

    return {"hue": hue_val, "sat": sat_val}
