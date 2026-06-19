#!/usr/bin/env python3
"""Push real-world light positions into the bridge's entertainment area.

Reads `positions.json` (real-world coordinates per light name, any consistent
unit), normalizes them into the Hue entertainment cube (-1..1 on each axis)
with a SINGLE uniform scale so the room's real proportions are preserved, then
PUTs the updated positions to the bridge's entertainment configuration.

Usage:
    uv run python apply_positions.py            # normalize + push
    uv run python apply_positions.py --dry-run  # print normalized coords only
"""

import json
import sys
from pathlib import Path

import requests
from dotenv import dotenv_values

requests.packages.urllib3.disable_warnings()

ROOT = Path(__file__).parent
POSITIONS_FILE = ROOT / "positions.json"


def normalize(raw: dict[str, list[float]]) -> dict[str, dict]:
    """Map real-world (x, y, z) into the -1..1 cube with one uniform scale.

    Each axis is centered on its own midpoint (a translation, so proportions are
    untouched), then ALL axes are scaled by the same factor — the one that makes
    the widest axis span exactly [-1, 1]. Smaller axes stay proportionally
    smaller, so distances stay physically faithful in every direction.
    """
    names = list(raw)
    axes = list(zip(*(raw[n] for n in names)))  # [(all x), (all y), (all z)]
    mids = [(min(a) + max(a)) / 2 for a in axes]
    spans = [max(a) - min(a) for a in axes]
    widest = max(spans) or 1.0
    scale = 2.0 / widest

    out: dict[str, dict] = {}
    for n in names:
        x, y, z = raw[n]
        out[n] = {
            "x": round((x - mids[0]) * scale, 4),
            "y": round((y - mids[1]) * scale, 4),
            "z": round((z - mids[2]) * scale, 4),
        }
    return out


def _name_key(name: str) -> str:
    return name.strip().lower()


def main():
    dry_run = "--dry-run" in sys.argv

    data = json.loads(POSITIONS_FILE.read_text())
    raw = data["positions"]
    normed = normalize(raw)

    print(f"Normalized positions (uniform scale, from {data.get('units', '?')}):")
    for name, p in normed.items():
        print(f"  {name:14s} x={p['x']:+.3f}  y={p['y']:+.3f}  z={p['z']:+.3f}")

    if dry_run:
        return

    env = dotenv_values(ROOT / ".env")
    ip, key = env["HUE_BRIDGE_IP"], env["HUE_API_KEY"]
    headers = {"hue-application-key": key}
    base = f"https://{ip}/clip/v2/resource"

    # name -> v1 light id
    lights = requests.get(
        f"https://{ip}/api/{key}/lights", verify=False, timeout=8
    ).json()
    name_to_v1 = {_name_key(info["name"]): int(lid) for lid, info in lights.items()}

    # v1 light id -> entertainment service rid
    svcs = requests.get(
        f"{base}/entertainment", headers=headers, verify=False, timeout=8
    ).json()["data"]
    v1_to_rid = {
        int(s["id_v1"].split("/")[-1]): s["id"]
        for s in svcs
        if (s.get("id_v1") or "").startswith("/lights/") and s.get("renderer")
    }

    # name -> rid
    rid_by_name = {}
    for name in normed:
        v1 = name_to_v1.get(_name_key(name))
        if v1 is None or v1 not in v1_to_rid:
            print(
                f"  WARNING: '{name}' not found on bridge / not entertainment-capable"
            )
            continue
        rid_by_name[name] = v1_to_rid[v1]

    # Load the (single) entertainment configuration and patch its positions
    configs = requests.get(
        f"{base}/entertainment_configuration", headers=headers, verify=False, timeout=8
    ).json()["data"]
    if not configs:
        print("ERROR: no entertainment area on the bridge. Create one first.")
        sys.exit(1)
    config = configs[0]
    config_id = config["id"]

    rid_to_pos = {rid_by_name[n]: normed[n] for n in rid_by_name}
    service_locations = []
    for sl in config["locations"]["service_locations"]:
        rid = sl["service"]["rid"]
        pos = rid_to_pos.get(rid)
        if pos is None:
            # keep existing position if we have no measurement for it
            pos = sl.get("position", {"x": 0.0, "y": 0.0, "z": 0.0})
        service_locations.append(
            {
                "service": sl["service"],
                "positions": [pos],
                "equalization_factor": sl.get("equalization_factor", 1.0),
            }
        )

    r = requests.put(
        f"{base}/entertainment_configuration/{config_id}",
        headers=headers,
        json={"locations": {"service_locations": service_locations}},
        verify=False,
        timeout=10,
    )
    print(f"\nPUT entertainment_configuration -> HTTP {r.status_code}")
    if r.status_code >= 300:
        print(json.dumps(r.json(), indent=2)[:500])
        sys.exit(1)

    # Read back and confirm
    config = requests.get(
        f"{base}/entertainment_configuration/{config_id}",
        headers=headers,
        verify=False,
        timeout=8,
    ).json()["data"][0]
    rid_to_name = {v: k for k, v in rid_by_name.items()}
    print("Bridge now reports:")
    for ch in config["channels"]:
        rid = ch["members"][0]["service"]["rid"] if ch.get("members") else None
        p = ch["position"]
        print(
            f"  ch {ch['channel_id']}  {rid_to_name.get(rid, '?'):14s} "
            f"x={p['x']:+.3f}  y={p['y']:+.3f}  z={p['z']:+.3f}"
        )
    print("\nDone. Trigger an effect reload to pick up new positions.")


if __name__ == "__main__":
    main()
