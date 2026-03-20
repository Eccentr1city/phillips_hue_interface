# Philips Hue Interface

A Python library for controlling Philips Hue lights, with support for real-time streaming effects via DTLS. Designed as the lighting backend for [Hob](https://github.com/Eccentr1city/hob), a Claude-based home assistant.

## Setup

**Requirements:** Python 3.12+, [uv](https://docs.astral.sh/uv/)

### 1. Install dependencies

```bash
uv sync
```

### 2. Pair with your Hue bridge

```bash
uv run python setup.py
```

This discovers your bridge, asks you to press the link button, registers API credentials (including an entertainment client key for streaming), and writes them to `.env`.

### 3. Create an entertainment area

Streaming effects require an entertainment area configured on the bridge. The Hue app can create one (Settings > Entertainment areas), or you can create one programmatically via the v2 API.

## Usage

### As a Python library

```python
from hue import Bridge

b = Bridge()                          # loads .env credentials
b.light(1).set(color="red")           # by ID
b.light("Desk").set(brightness=0.5)   # by name
b.light(1).off()
```

### CLI

```bash
uv run hue status                         # show all lights
uv run hue set all --color red             # set color
uv run hue set 1 --brightness 0.5          # set brightness
uv run hue set all --effect candle         # start streaming effect
uv run hue on                              # all lights on
uv run hue off 1                           # turn off light 1
uv run hue list                            # list effects and scenes
uv run hue scene save cozy                 # snapshot current state
uv run hue scene set cozy                  # restore a saved scene
uv run hue stop                            # stop streaming effects
```

### Hob integration

The library exports `TOOL_DEFINITIONS` and `TOOL_FUNCTIONS` from `hue.tools` for Claude tool-calling. Hob's `hue_tools.py` imports these:

```python
import sys
sys.path.insert(0, "/path/to/phillips_hue_interface")
from hue.tools import TOOL_DEFINITIONS, TOOL_FUNCTIONS
```

**Tools:** `hue_status`, `hue_set`, `hue_stop`, `hue_list`, `hue_define_effect`, `hue_define_scene`

## Effects

Effects are Python files that define a `render(t, **params) -> (r, g, b)` function where `t` is elapsed seconds and RGB values are 0-255. Each light automatically gets a unique `phase` parameter so they vary independently.

**Built-in effects** live in `hue/effects/`:
- `candle` -- realistic fire flicker with layered noise
- `breathe` -- slow color breathing

**User-defined effects** are created by Hob via `hue_define_effect` and saved to `effects/`. Hob can invent arbitrary effects on the fly.

### Example effect

```python
import math

def render(t, speed=1.0, phase=0.0, **params):
    wave = (math.sin(t * speed + phase) + 1) / 2
    r = int(255 * wave)
    g = int(100 * (1 - wave))
    b = 50
    return (r, g, b)
```

## Streaming architecture

Effects run at 25fps over a DTLS connection to the Hue bridge's Entertainment API. The streaming engine is a **persistent daemon** that stays alive across effect switches:

- **First effect request:** launches `python -m hue.stream` as a background subprocess. It connects to the bridge via DTLS once.
- **Subsequent effect requests:** writes new config to `.hue_stream_config.json` and sends `SIGUSR1`. The daemon hot-swaps effects without reconnecting. This is instant.
- **Stop:** sends `SIGTERM`. The daemon closes the DTLS session and exits.
- **Crash recovery:** if the daemon dies (bridge reboot, network error, bug), the next effect request detects the stale PID and launches a new daemon automatically. The daemon also auto-reconnects internally if the DTLS connection drops mid-stream.

### Files

| File | Purpose |
|------|---------|
| `.hue_stream.pid` | PID of the running daemon |
| `.hue_stream.log` | Daemon log (timestamps + events) |
| `.hue_stream_config.json` | Current effect config (read by daemon on SIGUSR1) |

All three are gitignored and created/cleaned up automatically.

### Troubleshooting

**Check if the daemon is running:**
```bash
cat .hue_stream.pid && ps aux | grep hue.stream
```

**View daemon logs:**
```bash
cat .hue_stream.log
```

**Force restart the daemon:**
```bash
kill $(cat .hue_stream.pid 2>/dev/null); rm -f .hue_stream.pid
# Next effect request will launch a new daemon
```

**DTLS handshake fails:**
The bridge only allows one entertainment session. If another app holds the session, the daemon will retry 5 times with backoff. If all attempts fail, it waits 10 seconds and tries again indefinitely.

## Scenes

Scenes are JSON files in `scenes/` mapping light IDs to configurations. They can include static colors and/or streaming effects.

```json
{
  "name": "movie",
  "lights": {
    "1": {"color": "blue", "brightness": 0.3},
    "2": {"effect": "breathe", "params": {"speed": 0.5}}
  }
}
```

Save the current light state as a scene:
```bash
uv run hue scene save mysetup
```

## Project structure

```
phillips_hue_interface/
  setup.py              # Bridge pairing script
  pyproject.toml        # Project config and dependencies
  .env                  # Bridge credentials (gitignored)
  hue/
    __init__.py         # Exports Bridge
    bridge.py           # Bridge connection, REST API, light discovery
    light.py            # Light model: .set(), .on(), .off(), color resolution
    tools.py            # Hob tool definitions (TOOL_DEFINITIONS + TOOL_FUNCTIONS)
    scene.py            # Scene save/load/apply
    stream.py           # Streaming daemon (DTLS, hot-reload, auto-reconnect)
    cli.py              # CLI entry point
    effects/
      __init__.py       # Effect loader (discovers built-in + user effects)
      candle.py         # Built-in: fire flicker
      breathe.py        # Built-in: color breathing
  effects/              # User-defined effects (created by Hob)
  scenes/               # Saved scenes (JSON)
```
