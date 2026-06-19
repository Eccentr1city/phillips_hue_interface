"""Beat-sync — drive the lights from live audio captured off the Mac.

Captures a digital audio loopback (e.g. BlackHole) and drives the lights from
two frequency bands at once:

- ANCHOR lights ride the deep-bass/kick band with a slow, smooth swell — the
  steady heartbeat of the room.
- FLAVOR lights ride a higher band (mids/highs) with a faster, twitchier
  response and their own color — accents layered on top.

This is a loopback tap of whatever the Mac is playing (Spotify, a soft-synth /
MIDI keyboard, a browser) — NOT a microphone, so it's immune to room noise and
works with headphones, as long as the audio routes through the Mac.

Setup (macOS): install BlackHole, then in Audio MIDI Setup create a
"Multi-Output Device" containing both your headphones/speakers AND BlackHole,
and select it as the system output. Run `hue beatsync --list` to find the
BlackHole input device.

Usage:
    hue beatsync --list
    hue beatsync [--device <name>] [--color red] [--flavor-color amber]
                 [--flavor "Desk,Bedside"] [--max-bright 0.22] [--min-bright 0.04]
                 [--sensitivity 1.7] [--refractory 0.34] [--decay 0.30]
                 [--attack 0.16] [--gain 0]
"""

import collections
import sys
import time

import numpy as np
import requests
import sounddevice as sd
from dotenv import dotenv_values

from hue.stream import _build_channel_maps, _send_frame

requests.packages.urllib3.disable_warnings()

BLOCKSIZE = 1024  # samples per audio block (~23ms at 44.1kHz)

# Flavor band/response (internal defaults): higher frequencies, faster + busier.
FLAVOR_BAND = (1500, 7000)
FLAVOR_SENSITIVITY = 1.5
FLAVOR_REFRACTORY = 0.10
FLAVOR_DECAY = 0.12
FLAVOR_ATTACK = 0.05


def list_devices():
    print("Audio input devices:")
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            print(f"  [{i}] {d['name']}  (in:{d['max_input_channels']})")
    print("\nPick the BlackHole device for loopback capture.")


def _resolve_device(spec):
    if spec is None:
        return None
    try:
        return int(spec)
    except (TypeError, ValueError):
        pass
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0 and spec.lower() in d["name"].lower():
            return i
    raise SystemExit(f"No input device matching '{spec}'. Try `hue beatsync --list`.")


_COLORS = {
    "red": (255, 0, 0),
    "warm": (255, 80, 15),
    "amber": (255, 140, 40),
    "orange": (255, 50, 0),
    "blue": (40, 90, 255),
    "green": (0, 255, 40),
    "purple": (150, 0, 255),
    "pink": (255, 40, 120),
    "white": (255, 255, 255),
}


def _parse_color(spec):
    """Resolve a color name or 'r,g,b' string to an (r, g, b) tuple."""
    if isinstance(spec, (tuple, list)):
        return tuple(spec)
    s = str(spec).strip().lower()
    if s in _COLORS:
        return _COLORS[s]
    if "," in s:
        parts = [int(float(x)) for x in s.split(",")][:3]
        if len(parts) == 3:
            return tuple(parts)
    return _COLORS["red"]


def _resolve_light_ids(spec, name_to_id):
    """Parse a comma list of light names/IDs into a set of v1 light IDs."""
    ids = set()
    for tok in str(spec).split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            ids.add(int(tok))
        except ValueError:
            lid = name_to_id.get(tok.lower())
            if lid is not None:
                ids.add(lid)
    return ids


class BandOnset:
    """Onset detector for one frequency band, with an adaptive threshold.

    Tracks recent band energy; an onset is a local spike above `sensitivity` x
    the recent average, gated by a loudness floor and a refractory period.
    """

    def __init__(self, samplerate, lo, hi, sensitivity, refractory, floor):
        freqs = np.fft.rfftfreq(BLOCKSIZE, 1.0 / samplerate)
        self._band = (freqs >= lo) & (freqs <= hi)
        self._window = np.hanning(BLOCKSIZE)
        self._hist = collections.deque(maxlen=int(samplerate / BLOCKSIZE * 0.7))
        self.sensitivity = sensitivity
        self.refractory = refractory
        self.floor = floor
        self.level = 0.0  # smoothed overall RMS
        self.last_beat = -1e9

    def process(self, mono, now):
        if len(mono) != BLOCKSIZE:
            return
        rms = float(np.sqrt(np.mean(mono**2)))
        self.level = 0.9 * self.level + 0.1 * rms
        mag = np.abs(np.fft.rfft(mono * self._window))
        energy = float(np.sum(mag[self._band] ** 2))
        if self._hist and self.level > self.floor:
            avg = sum(self._hist) / len(self._hist)
            if energy > self.sensitivity * avg and now - self.last_beat > self.refractory:
                self.last_beat = now
        self._hist.append(energy)


def _connect(ip, api_key, client_key):
    import hue_entertainment_pykit as hep

    bridge = hep.create_bridge(
        identification="",
        rid="",
        ip_address=ip,
        swversion=0,
        username=api_key,
        hue_app_id="phillips_hue_interface",
        clientkey=client_key,
        name="Hue Bridge",
    )
    ent = hep.Entertainment(bridge)
    configs = ent.get_entertainment_configs()
    if not configs:
        raise SystemExit("No entertainment area on the bridge.")
    config_id = list(configs.keys())[0]
    streaming = hep.Streaming(bridge, configs[config_id], ent.get_ent_conf_repo())
    streaming.set_color_space("rgb")
    streaming.start_stream()
    return streaming


def run(
    device=None,
    color="red",
    flavor_color="amber",
    flavor=None,
    sensitivity=1.7,
    refractory=0.34,
    decay=0.30,
    attack=0.16,
    min_bright=0.04,
    max_bright=0.22,
    gain=0.0,
    floor=0.04,
):
    """Capture audio and drive anchor + flavor lights until interrupted."""
    env = dotenv_values(".env")
    ip, api_key, client_key = (
        env["HUE_BRIDGE_IP"],
        env["HUE_API_KEY"],
        env["HUE_CLIENT_KEY"],
    )

    from hue.smooth import stop_smooth
    from hue.stream import stop_stream

    stop_stream()
    stop_smooth()

    dev = _resolve_device(device)
    info = sd.query_devices(dev if dev is not None else sd.default.device[0])
    samplerate = int(info["default_samplerate"])
    channels = min(2, info["max_input_channels"]) or 1
    print(f"Capturing from [{dev}] {info['name']} @ {samplerate}Hz, {channels}ch")

    bass_rgb = _parse_color(color)
    flavor_rgb = _parse_color(flavor_color)
    bass = BandOnset(samplerate, 30, 120, sensitivity, refractory, floor)
    flav = BandOnset(
        samplerate, *FLAVOR_BAND, FLAVOR_SENSITIVITY, FLAVOR_REFRACTORY, floor
    )

    def audio_cb(indata, frames, time_info, status):
        mono = indata.mean(axis=1) if indata.ndim > 1 else indata
        m = np.asarray(mono, dtype=np.float64)
        now = time.monotonic()
        bass.process(m, now)
        flav.process(m, now)

    streaming = _connect(ip, api_key, client_key)
    light_to_channel, _ = _build_channel_maps(ip, api_key)
    all_channels = sorted(light_to_channel.values())

    # Assign each light to anchor (bass) or flavor. Default: interleave so the
    # flavor accents are spread around the room.
    if flavor:
        lights = requests.get(
            f"https://{ip}/api/{api_key}/lights", verify=False, timeout=8
        ).json()
        name_to_id = {v["name"].strip().lower(): int(k) for k, v in lights.items()}
        flavor_ids = _resolve_light_ids(flavor, name_to_id)
        flavor_channels = {
            light_to_channel[i] for i in flavor_ids if i in light_to_channel
        }
    else:
        flavor_channels = set(all_channels[1::2])
    print(
        f"Anchor (bass) channels: {sorted(set(all_channels) - flavor_channels)} | "
        f"Flavor channels: {sorted(flavor_channels)}. Ctrl-C to stop."
    )

    fps = 50
    interval = 1.0 / fps
    bass_k = 1.0 - np.exp(-interval / max(1e-3, attack))
    flav_k = 1.0 - np.exp(-interval / max(1e-3, FLAVOR_ATTACK))
    bass_env = 0.0
    flav_env = 0.0

    stream = sd.InputStream(
        device=dev,
        channels=channels,
        samplerate=samplerate,
        blocksize=BLOCKSIZE,
        callback=audio_cb,
    )
    try:
        with stream:
            next_frame = time.monotonic()
            while True:
                now = time.monotonic()
                ib = np.exp(-(now - bass.last_beat) / decay)
                bass_env += (ib - bass_env) * bass_k
                ifl = np.exp(-(now - flav.last_beat) / FLAVOR_DECAY)
                flav_env += (ifl - flav_env) * flav_k

                span = max_bright - min_bright
                bass_b = min(max_bright, min_bright + span * bass_env + gain * bass.level)
                flav_b = min(max_bright, min_bright + span * flav_env)
                bass_px = tuple(c * bass_b for c in bass_rgb)
                flav_px = tuple(c * flav_b for c in flavor_rgb)

                frame = [
                    (cid, *(flav_px if cid in flavor_channels else bass_px))
                    for cid in all_channels
                ]
                _send_frame(streaming, frame)

                next_frame += interval
                sleep = next_frame - time.monotonic()
                if sleep > 0:
                    time.sleep(sleep)
                else:
                    next_frame = time.monotonic()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        try:
            streaming.stop_stream()
        except Exception:
            pass


if __name__ == "__main__":
    if "--list" in sys.argv:
        list_devices()
    else:
        run()
