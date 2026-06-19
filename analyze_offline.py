#!/usr/bin/env python3
"""Offline song analyzer — runs in .beatenv (Python 3.9 + madmom).

Reads an audio file and runs the FULL-SONG downbeat tracker (much more accurate
than the real-time tracker: global Viterbi over the whole track, reliable
downbeats, no edge/latency effects), then writes a "light score" JSON:

    {
      "version": 1,
      "duration": 213.4,
      "tempo": 122.0,
      "beats_per_bar": 4,
      "beats": [{"t": 0.51, "pos": 1}, {"t": 0.99, "pos": 2}, ...]
    }

`pos` is the bar position (1 = downbeat). Decoding handles any format madmom can
read (wav natively; mp3/m4a/etc. via ffmpeg).

Usage: python analyze_offline.py <audio_file> <out.json>
"""

import json
import sys

import numpy as np


def main():
    infile, outfile = sys.argv[1], sys.argv[2]
    from madmom.audio.signal import Signal
    from madmom.features.downbeats import (
        DBNDownBeatTrackingProcessor,
        RNNDownBeatProcessor,
    )

    sig = Signal(infile, sample_rate=44100, num_channels=1)
    act = RNNDownBeatProcessor()(sig)
    beats = DBNDownBeatTrackingProcessor(
        beats_per_bar=[3, 4], min_bpm=60, max_bpm=150, fps=100
    )(act)

    times = beats[:, 0]
    positions = beats[:, 1].astype(int)
    tempo = round(60.0 / float(np.median(np.diff(times))), 1) if len(times) >= 2 else 0.0
    score = {
        "version": 1,
        "duration": round(len(sig) / float(sig.sample_rate), 3),
        "tempo": tempo,
        "beats_per_bar": int(positions.max()) if len(positions) else 4,
        "beats": [{"t": round(float(t), 4), "pos": int(p)} for t, p in zip(times, positions)],
    }
    with open(outfile, "w") as f:
        json.dump(score, f)
    print(
        f"analyzed: {len(times)} beats, tempo {tempo} BPM, "
        f"{score['duration']:.1f}s, beats_per_bar {score['beats_per_bar']}",
        flush=True,
    )


if __name__ == "__main__":
    main()
