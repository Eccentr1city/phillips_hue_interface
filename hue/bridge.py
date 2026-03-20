"""Bridge connection, light discovery, and REST API helpers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import requests
from dotenv import load_dotenv

if TYPE_CHECKING:
    from hue.light import Light

# Suppress SSL warnings for local bridge communication
requests.packages.urllib3.disable_warnings()

# Well-known color names → (hue, saturation) for Hue API (hue: 0-65535, sat: 0-254)
COLOR_MAP = {
    "red": (0, 254),
    "orange": (5000, 254),
    "yellow": (10000, 254),
    "green": (25500, 254),
    "cyan": (36000, 254),
    "blue": (46920, 254),
    "purple": (50000, 254),
    "pink": (56100, 200),
    "white": (0, 0),
    "warm white": (8000, 140),
    "cool white": (0, 50),
}


class Bridge:
    """Connection to a Philips Hue bridge."""

    def __init__(
        self,
        ip: str | None = None,
        api_key: str | None = None,
        client_key: str | None = None,
    ):
        # Load .env from the package root (where pyproject.toml lives)
        env_path = Path(__file__).resolve().parent.parent / ".env"
        load_dotenv(env_path)

        self.ip = ip or os.environ.get("HUE_BRIDGE_IP", "")
        self.api_key = api_key or os.environ.get("HUE_API_KEY", "")
        self.client_key = client_key or os.environ.get("HUE_CLIENT_KEY", "")

        if not self.ip or not self.api_key:
            raise RuntimeError(
                "Missing HUE_BRIDGE_IP or HUE_API_KEY. Run setup.py first."
            )

        self._base_url = f"https://{self.ip}/api/{self.api_key}"
        self._lights: dict[int, "Light"] | None = None

    def _get(self, path: str) -> dict:
        resp = requests.get(f"{self._base_url}{path}", verify=False, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _put(self, path: str, data: dict) -> list:
        resp = requests.put(
            f"{self._base_url}{path}", json=data, verify=False, timeout=10
        )
        resp.raise_for_status()
        return resp.json()

    def discover_lights(self) -> dict[int, "Light"]:
        """Fetch all lights from the bridge and return as Light objects."""
        from hue.light import Light

        raw = self._get("/lights")
        lights = {}
        for lid_str, info in raw.items():
            lid = int(lid_str)
            lights[lid] = Light(
                bridge=self, id=lid, name=info.get("name", f"Light {lid}"), raw=info
            )
        self._lights = lights
        return lights

    @property
    def lights(self) -> dict[int, "Light"]:
        if self._lights is None:
            self.discover_lights()
        return self._lights

    def light(self, id_or_name: int | str) -> "Light":
        """Get a light by ID (int) or name (str)."""
        lights = self.lights
        if isinstance(id_or_name, int):
            if id_or_name not in lights:
                raise KeyError(f"No light with ID {id_or_name}")
            return lights[id_or_name]
        # Search by name (case-insensitive)
        for light in lights.values():
            if light.name.lower() == id_or_name.lower():
                return light
        raise KeyError(f"No light named '{id_or_name}'")

    @property
    def all(self) -> "AllLights":
        return AllLights(self)


class AllLights:
    """Proxy for controlling all lights at once."""

    def __init__(self, bridge: Bridge):
        self._bridge = bridge

    def set(self, **kwargs):
        for light in self._bridge.lights.values():
            light.set(**kwargs)

    def on(self):
        for light in self._bridge.lights.values():
            light.on()

    def off(self):
        for light in self._bridge.lights.values():
            light.off()
