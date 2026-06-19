"""Beat-sync — drive the lights from live audio captured off the Mac.

Captures a digital audio loopback (e.g. BlackHole) and drives the lights from
two frequency bands at once:

- ANCHOR lights ride the deep-bass/kick band with a slow, smooth swell that
  tracks the fundamental pulse (continuous energy-vs-recent-average strength).
- FLAVOR lights ride a higher band (mids/highs) with faster accents in their
  own color, capped dimmer so they sit under the bass.

Loopback tap (NOT a microphone): immune to room noise, works with headphones,
as long as audio routes through the Mac. Setup: install BlackHole, make a
Multi-Output Device (your output + BlackHole), select it as system output.

The BeatSyncEngine runs the capture + render loop in a background thread and
accepts live parameter updates (used by the web UI). `run()` is the CLI wrapper.

Usage:
    hue beatsync --list
    hue beatsync [--device <name>] [--color red] [--flavor-color amber]
                 [--flavor "Wall closet,Wall right"] [--max-bright 0.22]
                 [--flavor-max 0.10] [--attack 0.10] [--decay 0.35]
"""

import collections
import sys
import threading
import time

import numpy as np
import requests
import sounddevice as sd
from dotenv import dotenv_values

from hue.stream import _build_channel_maps, _send_frame

requests.packages.urllib3.disable_warnings()

BLOCKSIZE = 1024
FLAVOR_BAND = (1500, 7000)
FLAVOR_SENSITIVITY = 1.5
FLAVOR_REFRACTORY = 0.13
FLAVOR_DECAY = 0.12
FLAVOR_ATTACK = 0.05

DEFAULTS = {
    "color": "red",
    "flavor_color": "amber",
    "flavor": "",  # comma list of names/IDs; rest are anchor
    "sensitivity": 1.7,
    "refractory": 0.34,
    "decay": 0.35,
    "attack": 0.10,
    "min_bright": 0.04,
    "max_bright": 0.22,
    "flavor_max": 0.10,
    "pulse_scale": 1.3,
    "gain": 0.0,
    "floor": 0.04,
}

# Slider/param spec for the web UI.
PARAMS = [
    {"name": "max_bright", "label": "Anchor brightness", "min": 0.02, "max": 0.6, "step": 0.01},
    {"name": "flavor_max", "label": "Flavor brightness", "min": 0.0, "max": 0.4, "step": 0.01},
    {"name": "min_bright", "label": "Baseline glow", "min": 0.0, "max": 0.2, "step": 0.005},
    {"name": "attack", "label": "Attack (s)", "min": 0.02, "max": 0.5, "step": 0.01},
    {"name": "decay", "label": "Release (s)", "min": 0.05, "max": 1.0, "step": 0.01},
    {"name": "pulse_scale", "label": "Bass pulse scale", "min": 0.3, "max": 3.0, "step": 0.05},
    {"name": "sensitivity", "label": "Flavor sensitivity", "min": 1.05, "max": 3.0, "step": 0.05},
    {"name": "refractory", "label": "Anchor refractory (s)", "min": 0.05, "max": 0.8, "step": 0.01},
    {"name": "gain", "label": "Loudness glow", "min": 0.0, "max": 5.0, "step": 0.1},
    {"name": "color", "label": "Anchor color", "type": "color"},
    {"name": "flavor_color", "label": "Flavor color", "type": "color"},
    {"name": "flavor", "label": "Flavor lights (names)", "type": "text"},
]


def list_devices():
    print("Audio input devices:")
    for d in input_devices():
        print(f"  [{d['index']}] {d['name']}  (in:{d['channels']})")
    print("\nPick the BlackHole device for loopback capture.")


def input_devices():
    return [
        {"index": i, "name": d["name"], "channels": d["max_input_channels"]}
        for i, d in enumerate(sd.query_devices())
        if d["max_input_channels"] > 0
    ]


def _resolve_device(spec):
    if spec is None or spec == "":
        return None
    try:
        return int(spec)
    except (TypeError, ValueError):
        pass
    for d in input_devices():
        if spec.lower() in d["name"].lower():
            return d["index"]
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
    """Per-band detector: continuous pulse strength + discrete onset times."""

    def __init__(self, samplerate, lo, hi, sensitivity, refractory, floor):
        freqs = np.fft.rfftfreq(BLOCKSIZE, 1.0 / samplerate)
        self._band = (freqs >= lo) & (freqs <= hi)
        self._window = np.hanning(BLOCKSIZE)
        self._hist = collections.deque(maxlen=int(samplerate / BLOCKSIZE * 0.7))
        self.sensitivity = sensitivity
        self.refractory = refractory
        self.floor = floor
        self.level = 0.0
        self.last_beat = -1e9
        self._avg = 1e-9
        self.strength = 0.0

    def process(self, mono, now):
        if len(mono) != BLOCKSIZE:
            return
        rms = float(np.sqrt(np.mean(mono**2)))
        self.level = 0.9 * self.level + 0.1 * rms
        mag = np.abs(np.fft.rfft(mono * self._window))
        energy = float(np.sum(mag[self._band] ** 2))
        if self.level > self.floor:
            self.strength = max(0.0, energy / (self._avg + 1e-12) - 1.0)
        else:
            self.strength = 0.0
        self._avg = 0.95 * self._avg + 0.05 * energy
        if self._hist and self.level > self.floor:
            avg = sum(self._hist) / len(self._hist)
            if energy > self.sensitivity * avg and now - self.last_beat > self.refractory:
                self.last_beat = now
        self._hist.append(energy)


class BeatTracker:
    """Beat tracker backed by librosa's dynamic-programming beat tracker.

    A background thread runs `librosa.beat.beat_track` on a rolling audio window
    a few times a second (cheap on a fast machine, heavy-ish algorithmically) to
    get tempo + beat positions, then phase-locks a beat grid that the render loop
    reads via `seconds_since_beat`. The grid advances on its own clock between
    analyses, so beats are predicted through quiet bars.
    """

    def __init__(self, samplerate, window_s=8.0, update_s=0.4):
        self.sr = samplerate
        self.update_s = update_s
        self._maxlen = int(samplerate * window_s)
        self._ring = np.zeros(self._maxlen, dtype=np.float32)
        self._widx = 0
        self._filled = 0
        self._last_time = 0.0
        self._lock = threading.Lock()
        self.period = None  # seconds per beat
        self.beat_ref = 0.0  # a known time that lands on a beat
        self.bpm = 0.0
        self.ready = False
        self._stop = threading.Event()
        self._thread = None

    def push(self, mono, now):
        n = len(mono)
        with self._lock:
            if self._widx + n <= self._maxlen:
                self._ring[self._widx : self._widx + n] = mono
            else:
                k = self._maxlen - self._widx
                self._ring[self._widx :] = mono[:k]
                self._ring[: n - k] = mono[k:]
            self._widx = (self._widx + n) % self._maxlen
            self._filled = min(self._maxlen, self._filled + n)
            self._last_time = now

    def _snapshot(self):
        with self._lock:
            if self._filled < self._maxlen:
                y = self._ring[: self._filled].copy()
            else:
                y = np.concatenate((self._ring[self._widx :], self._ring[: self._widx]))
            return y, self._last_time

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _worker(self):
        import librosa  # imported here so it doesn't slow process startup

        while not self._stop.is_set():
            time.sleep(self.update_s)
            y, t_last = self._snapshot()
            if len(y) < self.sr * 3 or not np.any(y):
                continue
            try:
                tempo, beats = librosa.beat.beat_track(y=y, sr=self.sr, units="time")
            except Exception:
                continue
            bpm = float(np.atleast_1d(tempo)[0])
            if bpm <= 0 or len(beats) < 2:
                self.ready = False
                continue
            period = 60.0 / bpm
            buf_start = t_last - len(y) / self.sr
            last_beat = buf_start + float(beats[-1])
            if self.period is None:
                self.period, self.beat_ref = period, last_beat
            else:
                self.period = 0.7 * self.period + 0.3 * period
                err = (last_beat - self.beat_ref) % self.period
                if err > self.period / 2:
                    err -= self.period
                self.beat_ref += 0.5 * err  # gentle PLL phase correction
            self.bpm = 60.0 / self.period
            self.ready = True

    def seconds_since_beat(self, now):
        """Time since the most recent grid beat, or None if no tempo yet."""
        if not self.ready or not self.period:
            return None
        return (now - self.beat_ref) % self.period


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
        raise RuntimeError("No entertainment area on the bridge.")
    config_id = list(configs.keys())[0]
    streaming = hep.Streaming(bridge, configs[config_id], ent.get_ent_conf_repo())
    streaming.set_color_space("rgb")
    streaming.start_stream()
    return streaming


class BeatSyncEngine:
    """Runs capture + render in a background thread; accepts live param updates."""

    def __init__(self):
        self.params = dict(DEFAULTS)
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        self.error = None

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def set_params(self, **kw):
        with self._lock:
            self.params.update({k: v for k, v in kw.items() if v is not None})

    def start(self, device=None, **params):
        if self.is_running():
            self.set_params(**params)
            return
        self.set_params(**params)
        self.error = None
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(device,), daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=8)
            self._thread = None

    def _snapshot(self):
        with self._lock:
            return dict(self.params)

    def _run(self, device):
        try:
            self._loop(device)
        except Exception as exc:  # surface to the UI rather than dying silently
            self.error = str(exc)

    def _loop(self, device):
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

        p = self._snapshot()
        bass = BandOnset(samplerate, 30, 120, p["sensitivity"], p["refractory"], p["floor"])
        flav = BandOnset(
            samplerate, *FLAVOR_BAND, FLAVOR_SENSITIVITY, FLAVOR_REFRACTORY, p["floor"]
        )
        tracker = BeatTracker(samplerate)
        tracker.start()

        def audio_cb(indata, frames, time_info, status):
            mono = indata.mean(axis=1) if indata.ndim > 1 else indata
            m = np.asarray(mono, dtype=np.float64)
            now = time.monotonic()
            bass.process(m, now)
            flav.process(m, now)
            tracker.push(m, now)

        streaming = _connect(ip, api_key, client_key)
        light_to_channel, _ = _build_channel_maps(ip, api_key)
        all_channels = sorted(light_to_channel.values())
        lights = requests.get(
            f"https://{ip}/api/{api_key}/lights", verify=False, timeout=8
        ).json()
        name_to_id = {v["name"].strip().lower(): int(k) for k, v in lights.items()}

        def compute_flavor_channels(spec):
            if spec:
                ids = _resolve_light_ids(spec, name_to_id)
                return {light_to_channel[i] for i in ids if i in light_to_channel}
            return set(all_channels[1::2])

        flavor_spec = p["flavor"]
        flavor_channels = compute_flavor_channels(flavor_spec)

        interval = 1.0 / 50
        bass_env = flav_env = 0.0
        stream = sd.InputStream(
            device=dev,
            channels=channels,
            samplerate=samplerate,
            blocksize=BLOCKSIZE,
            callback=audio_cb,
        )
        with stream:
            next_frame = time.monotonic()
            while not self._stop.is_set():
                p = self._snapshot()
                if p["flavor"] != flavor_spec:
                    flavor_spec = p["flavor"]
                    flavor_channels = compute_flavor_channels(flavor_spec)
                bass.sensitivity = p["sensitivity"]
                bass.refractory = p["refractory"]
                bass.floor = flav.floor = p["floor"]
                bass_rgb = _parse_color(p["color"])
                flav_rgb = _parse_color(p["flavor_color"])
                attack_k = 1.0 - np.exp(-interval / max(1e-3, p["attack"]))
                release_k = 1.0 - np.exp(-interval / max(1e-3, p["decay"]))
                flav_k = 1.0 - np.exp(-interval / max(1e-3, FLAVOR_ATTACK))

                now = time.monotonic()
                # Anchor target: a pulse on the tracked beat grid (predicts
                # through gaps); fall back to raw energy-follow when there's no
                # confident tempo yet (intro/ambient) or during near-silence.
                tsb = tracker.seconds_since_beat(now) if bass.level > p["floor"] else None
                if tsb is not None:
                    target = float(np.exp(-tsb / max(0.05, p["decay"])))
                else:
                    target = min(1.0, bass.strength / max(0.05, p["pulse_scale"]))
                bass_env += (target - bass_env) * (
                    attack_k if target > bass_env else release_k
                )
                ifl = np.exp(-(now - flav.last_beat) / FLAVOR_DECAY)
                flav_env += (ifl - flav_env) * flav_k

                mn, mx, fmx = p["min_bright"], p["max_bright"], p["flavor_max"]
                bass_b = min(mx, mn + (mx - mn) * bass_env + p["gain"] * bass.level)
                flav_b = min(fmx, mn + (fmx - mn) * flav_env)
                bass_px = tuple(c * bass_b for c in bass_rgb)
                flav_px = tuple(c * flav_b for c in flav_rgb)
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

        tracker.stop()
        try:
            streaming.stop_stream()
        except Exception:
            pass


def run(device=None, **params):
    """CLI entry: run the engine in the foreground until Ctrl-C."""
    engine = BeatSyncEngine()
    engine.start(device=device, **params)
    if engine.error:
        raise SystemExit(engine.error)
    print(
        f"beatsync running on device {device!r}. Ctrl-C to stop.\n"
        f"(Tune live in the browser with `hue ui`.)"
    )
    try:
        while engine.is_running():
            time.sleep(0.3)
            if engine.error:
                raise SystemExit(engine.error)
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        engine.stop()


if __name__ == "__main__":
    if "--list" in sys.argv:
        list_devices()
    else:
        run()
