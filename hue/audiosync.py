"""Beat-sync — drive the lights from live audio captured off the Mac.

Captures a digital audio loopback (e.g. BlackHole), detects beats from the
low-frequency (kick) energy, and streams a beat-triggered flash over a
loudness-tracking glow to the lights via the low-latency entertainment path.

This is a loopback tap of whatever the Mac is playing (Spotify, a soft-synth /
MIDI keyboard, a browser) — NOT a microphone, so it's immune to room noise and
works with headphones, as long as the audio routes through the Mac.

Setup (macOS): install BlackHole, then in Audio MIDI Setup create a
"Multi-Output Device" containing both your headphones/speakers AND BlackHole,
and select it as the system output. Run `hue beatsync --list` to find the
BlackHole input device, then `hue beatsync --device BlackHole`.

Usage:
    hue beatsync --list
    hue beatsync [--device <name>] [--color red] [--max-bright 0.22]
                 [--min-bright 0.04] [--sensitivity 1.6] [--refractory 0.22]
                 [--decay 0.16] [--gain 0]
"""

import collections
import sys
import time

import numpy as np
import sounddevice as sd
from dotenv import dotenv_values

from hue.stream import _build_channel_maps, _send_frame

BLOCKSIZE = 1024  # samples per audio block (~23ms at 44.1kHz)


def list_devices():
    print("Audio input devices:")
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            print(f"  [{i}] {d['name']}  (in:{d['max_input_channels']})")
    print("\nPick the BlackHole device for loopback capture.")


def _resolve_device(spec):
    """Resolve a device index or name-substring to an input device index."""
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


class BeatDetector:
    """Band-limited energy beat detector with an adaptive threshold.

    Tracks recent low-band (kick) energy; a beat is a local energy spike above
    `sensitivity` x the recent average, gated by a loudness floor and a
    refractory period. Also exposes a smoothed level and a bass/treble "tone".
    """

    def __init__(self, samplerate, sensitivity=1.6, refractory=0.22, floor=0.04):
        freqs = np.fft.rfftfreq(BLOCKSIZE, 1.0 / samplerate)
        self._bass = (freqs >= 20) & (freqs <= 150)
        self._treble = freqs >= 2000
        self._window = np.hanning(BLOCKSIZE)
        self._hist = collections.deque(maxlen=int(samplerate / BLOCKSIZE * 0.7))
        self.sensitivity = sensitivity
        self.refractory = refractory  # min seconds between beats
        self.floor = floor  # RMS noise gate
        self.level = 0.0  # smoothed RMS (0..~1)
        self.tone = 0.5  # 0 = all bass, 1 = all treble
        self.last_beat = -1e9

    def process(self, mono, now):
        if len(mono) != BLOCKSIZE:
            return
        rms = float(np.sqrt(np.mean(mono**2)))
        self.level = 0.9 * self.level + 0.1 * rms

        mag = np.abs(np.fft.rfft(mono * self._window))
        bass_e = float(np.sum(mag[self._bass] ** 2))
        treble_e = float(np.sum(mag[self._treble] ** 2))
        self.tone = 0.85 * self.tone + 0.15 * (treble_e / (bass_e + treble_e + 1e-12))

        if self._hist and self.level > self.floor:
            avg = sum(self._hist) / len(self._hist)
            if (
                bass_e > self.sensitivity * avg
                and now - self.last_beat > self.refractory
            ):
                self.last_beat = now
        self._hist.append(bass_e)


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


def run(
    device=None,
    sensitivity=1.6,
    refractory=0.22,
    decay=0.16,
    color="red",
    min_bright=0.04,
    max_bright=0.22,
    gain=0.0,
    floor=0.04,
):
    """Capture audio and drive the lights on the beat until interrupted."""
    env = dotenv_values(".env")
    ip, api_key, client_key = (
        env["HUE_BRIDGE_IP"],
        env["HUE_API_KEY"],
        env["HUE_CLIENT_KEY"],
    )

    # Don't fight the other daemons over the single entertainment session.
    from hue.smooth import stop_smooth
    from hue.stream import stop_stream

    stop_stream()
    stop_smooth()

    dev = _resolve_device(device)
    info = sd.query_devices(dev if dev is not None else sd.default.device[0])
    samplerate = int(info["default_samplerate"])
    channels = min(2, info["max_input_channels"]) or 1
    print(f"Capturing from [{dev}] {info['name']} @ {samplerate}Hz, {channels}ch")

    detector = BeatDetector(samplerate, sensitivity, refractory, floor)
    rgb = _parse_color(color)

    def audio_cb(indata, frames, time_info, status):
        mono = indata.mean(axis=1) if indata.ndim > 1 else indata
        detector.process(np.asarray(mono, dtype=np.float64), time.monotonic())

    streaming = _connect(ip, api_key, client_key)
    light_to_channel, _ = _build_channel_maps(ip, api_key)
    channel_ids = list(light_to_channel.values())
    print(f"Streaming to {len(channel_ids)} lights. Ctrl-C to stop.")

    fps = 50
    interval = 1.0 / fps

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
                # Beat flash envelope: spike at the beat, exponential decay.
                env_beat = np.exp(-(now - detector.last_beat) / decay)
                # Gentle dim baseline that pulses up to max_bright on each beat
                # (plus an optional loudness term via gain). Capped low so it
                # stays easy on the eyes.
                bright = min(
                    max_bright,
                    min_bright
                    + (max_bright - min_bright) * env_beat
                    + gain * detector.level,
                )
                r, g, b = (c * bright for c in rgb)

                frame = [(cid, r, g, b) for cid in channel_ids]
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
