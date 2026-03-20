#!/usr/bin/env python3
"""
Philips Hue Bridge setup script.

Run this when you're ready to press the link button on your Hue bridge.
It will:
  1. Discover the bridge on your network
  2. Wait for you to press the link button
  3. Register a new API user + entertainment client key
  4. Write credentials to .env
"""

import json
import sys
import time
from pathlib import Path

import requests

ENV_PATH = Path(__file__).parent / ".env"

# Suppress SSL warnings for local bridge communication
requests.packages.urllib3.disable_warnings()


def discover_bridge() -> str | None:
    """Find a Hue bridge via the cloud discovery endpoint."""
    print("Searching for Hue bridges on your network...")
    try:
        resp = requests.get("https://discovery.meethue.com", timeout=10)
        bridges = resp.json()
    except Exception as e:
        print(f"  Cloud discovery failed: {e}")
        return None

    if not bridges:
        print("  No bridges found.")
        return None

    if len(bridges) == 1:
        ip = bridges[0]["internalipaddress"]
        bridge_id = bridges[0].get("id", "unknown")
        print(f"  Found bridge {bridge_id} at {ip}")
        return ip

    # Multiple bridges — let the user pick
    print(f"  Found {len(bridges)} bridges:")
    for i, b in enumerate(bridges):
        print(f"    [{i + 1}] {b['internalipaddress']}  (id: {b.get('id', '?')})")
    while True:
        choice = input("  Which bridge? [1]: ").strip() or "1"
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(bridges):
                return bridges[idx]["internalipaddress"]
        except ValueError:
            pass
        print("  Invalid choice, try again.")


def register(bridge_ip: str) -> tuple[str, str] | None:
    """
    Register with the bridge. Returns (api_key, client_key) or None.

    The bridge must be in link mode (button pressed within last 30s).
    """
    url = f"https://{bridge_ip}/api"
    body = {
        "devicetype": "phillips_hue_interface#setup",
        "generateclientkey": True,
    }
    try:
        resp = requests.post(url, json=body, verify=False, timeout=5)
        result = resp.json()
    except Exception as e:
        print(f"  Request failed: {e}")
        return None

    if isinstance(result, list):
        result = result[0]

    if "error" in result:
        return None

    if "success" in result:
        return result["success"]["username"], result["success"]["clientkey"]

    return None


def write_env(bridge_ip: str, api_key: str, client_key: str):
    """Write (or overwrite) the .env file with bridge credentials."""
    # Preserve any extra lines (like ANTHROPIC_API_KEY) if .env already exists
    existing_lines = []
    keys_we_set = {"HUE_BRIDGE_IP", "HUE_API_KEY", "HUE_CLIENT_KEY"}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            key = line.split("=", 1)[0].strip()
            if key not in keys_we_set:
                existing_lines.append(line)

    lines = [
        f"HUE_BRIDGE_IP={bridge_ip}",
        f"HUE_API_KEY={api_key}",
        f"HUE_CLIENT_KEY={client_key}",
        *existing_lines,
    ]
    ENV_PATH.write_text("\n".join(lines) + "\n")


def main():
    print()
    print("=== Philips Hue Bridge Setup ===")
    print()

    # Step 1: Discover
    bridge_ip = discover_bridge()
    if not bridge_ip:
        manual = input("Enter bridge IP manually (or press Enter to quit): ").strip()
        if not manual:
            sys.exit(1)
        bridge_ip = manual

    # Verify bridge is reachable
    try:
        resp = requests.get(
            f"https://{bridge_ip}/api/config", verify=False, timeout=5
        )
        config = resp.json()
        print(f"  Bridge name: {config.get('name', '?')}")
        print(f"  Model:       {config.get('modelid', '?')}")
        print(f"  SW version:  {config.get('swversion', '?')}")
    except Exception:
        print(f"  Warning: could not reach bridge at {bridge_ip}")

    # Step 2: Pair
    print()
    print(">> Press the link button on your Hue bridge, then press Enter here.")
    input("   [waiting...] ")
    print()

    print("Registering...")
    # Try a few times in case the button press is slow to register
    for attempt in range(5):
        creds = register(bridge_ip)
        if creds:
            break
        if attempt < 4:
            print(f"  Bridge not ready, retrying ({attempt + 2}/5)...")
            time.sleep(2)

    if not creds:
        print("Failed to register. Make sure you pressed the link button recently.")
        sys.exit(1)

    api_key, client_key = creds
    print(f"  API key:    {api_key}")
    print(f"  Client key: {client_key}")

    # Step 3: Write .env
    write_env(bridge_ip, api_key, client_key)
    print(f"\nCredentials saved to {ENV_PATH}")

    # Step 4: Quick test — list lights
    print("\nDiscovering lights...")
    try:
        resp = requests.get(
            f"https://{bridge_ip}/api/{api_key}/lights", verify=False, timeout=5
        )
        lights = resp.json()
        if lights:
            print(f"  Found {len(lights)} light(s):")
            for lid, info in sorted(lights.items(), key=lambda x: int(x[0])):
                name = info.get("name", "?")
                light_type = info.get("type", "?")
                on = info.get("state", {}).get("on", "?")
                print(f"    [{lid}] {name}  ({light_type}, on={on})")
        else:
            print("  No lights found (they may need to be added in the Hue app first).")
    except Exception as e:
        print(f"  Could not list lights: {e}")

    print("\nSetup complete!")


if __name__ == "__main__":
    main()
