#!/usr/bin/env python3
"""Standalone madmom beat-tracking sidecar — runs in .beatenv (Python 3.9).

The main app runs on Python 3.13, but the SOTA beat trackers (madmom's RNN+DBN)
only work on Python <=3.9. So this tiny standalone process lives in an isolated
3.9 venv (.beatenv), captures the same audio loopback, runs madmom on a rolling
buffer, and streams the tempo + beat phase to the main process over local UDP as
JSON lines: {"bpm": float, "since": seconds_since_last_beat}.

Has NO dependency on the `hue` package (different interpreter); only madmom,
sounddevice, numpy. Launched by hue.audiosync.SidecarBeatTracker.

Usage: python beat_sidecar.py [--device BlackHole] [--port 9099]
"""

import argparse
import json
import socket
import threading
import time

import numpy as np
import sounddevice as sd
from scipy.signal import resample_poly


def resolve_device(spec):
    if spec in (None, ""):
        return None
    try:
        return int(spec)
    except ValueError:
        pass
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0 and spec.lower() in d["name"].lower():
            return i
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default=None)
    ap.add_argument("--port", type=int, default=9099)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--window", type=float, default=6.0)
    ap.add_argument("--interval", type=float, default=0.5)
    args = ap.parse_args()

    from madmom.audio.signal import Signal
    from madmom.features.downbeats import (
        DBNDownBeatTrackingProcessor,
        RNNDownBeatProcessor,
    )

    dev = resolve_device(args.device)
    info = sd.query_devices(dev if dev is not None else sd.default.device[0])
    sr = int(info["default_samplerate"])
    channels = min(2, info["max_input_channels"]) or 1
    maxlen = int(sr * args.window)

    ring = np.zeros(maxlen, dtype=np.float32)
    state = {"widx": 0, "filled": 0, "last_t": 0.0}
    lock = threading.Lock()

    def cb(indata, frames, time_info, status):
        m = indata.mean(axis=1) if indata.ndim > 1 else indata.reshape(-1)
        m = np.asarray(m, dtype=np.float32)
        n = len(m)
        now = time.monotonic()
        with lock:
            w = state["widx"]
            if w + n <= maxlen:
                ring[w : w + n] = m
            else:
                k = maxlen - w
                ring[w:] = m[:k]
                ring[: n - k] = m[k:]
            state["widx"] = (w + n) % maxlen
            state["filled"] = min(maxlen, state["filled"] + n)
            state["last_t"] = now

    rnn = RNNDownBeatProcessor()
    dbn = DBNDownBeatTrackingProcessor(beats_per_bar=[3, 4], fps=100)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    stream = sd.InputStream(
        device=dev, channels=channels, samplerate=sr, blocksize=1024, callback=cb
    )
    print(f"[sidecar] device={dev} sr={sr} -> udp {args.host}:{args.port}", flush=True)
    with stream:
        while True:
            time.sleep(args.interval)
            with lock:
                if state["filled"] < sr * 3:
                    continue
                if state["filled"] < maxlen:
                    y = ring[: state["filled"]].copy()
                else:
                    w = state["widx"]
                    y = np.concatenate((ring[w:], ring[:w]))
                tlast = state["last_t"]
            if not np.any(np.abs(y) > 1e-4):
                continue
            dur = len(y) / sr
            # madmom expects 44.1kHz and resamples via ffmpeg otherwise — do it
            # ourselves with scipy so there's no ffmpeg dependency.
            if sr != 44100:
                g = np.gcd(44100, sr)
                y = resample_poly(y, 44100 // g, sr // g).astype(np.float32)
            try:
                # beats: Nx2 array of [time_sec, position_in_bar] (1 = downbeat)
                beats = dbn(rnn(Signal(y, sample_rate=44100, num_channels=1)))
            except Exception as exc:
                print(f"[sidecar] error: {exc}", flush=True)
                continue
            if len(beats) < 2:
                continue
            times = beats[:, 0]
            period = float(np.median(np.diff(times)))
            if period <= 0:
                continue
            # Age of the last detected beat as of NOW — includes the analysis
            # time, so the main lands the grid on the beat rather than lagging by
            # however long madmom took to run.
            age = (time.monotonic() - tlast) + (dur - float(times[-1]))
            msg = {
                "period": round(period, 5),
                "age": round(age, 4),
                "pos": int(beats[-1, 1]),
                "bpb": int(beats[:, 1].max()),
            }
            sock.sendto(json.dumps(msg).encode(), (args.host, args.port))


if __name__ == "__main__":
    main()
