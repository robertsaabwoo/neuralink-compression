"""Bring-up pattern set: stimuli for the board and what they exercise (golden model only).

  python scripts/bringup/patterns.py                       coverage table of the set
  python scripts/bringup/patterns.py --emit DIR [--n-slots 32] [--slots 1,5,6,12,17,22,30,31]
        one .npy per pattern: full ADC frames (1 packet = 256 frames x n_slots), the pattern
        in the selected slots, seeded random filler elsewhere; plus config.json. Drive them
        with the TT pins, capture, then diagnose.py --stim DIR/<pattern>.npy --config ...

Per pattern: symbols hit per context (of 64 + escape), escapes, peak |coefficient| per level
against the RTL row widths, bytes per packet. The union over the set is what a stuck ROM
entry / row bit test can reach: a fault the set never exercises is invisible (README limits).
From the software-test study (branch swtest-study): 13 synthetic patterns + the recordings
cover 64/64 symbols in all 4 contexts.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "model"))
from nlc import stimgen  # noqa: E402
from nlc.data import load_challenge, synthetic  # noqa: E402
from nlc.lossy import LossyConfig, channel_symbols, stream_coeffs, symbol_of  # noqa: E402

CFG = LossyConfig()
T = CFG.frames_per_packet
CTX = {0: "a3", 1: "d1", 2: "d2", 3: "d3"}
# lifting value bounds the RTL rows are sized for (datapath.ROW_W): |d1|,|d2|,|d3|,|a3|
WIDTH_BOUND = [1023, 2046, 4092, 4092]
NAMES = ["silence", "dc0", "dc1023", "ramp", "ramp_small", "impulse", "step", "square_nyq",
         "square_16", "sine_fs", "noise_amp", "lfsr", "synthetic", "real"]


def pat(name: str, n_ch: int = 8, t: int = T, raw: Path | None = None, seed: int = 0):
    f = np.arange(t)[:, None]
    c = np.arange(n_ch)[None, :]
    if name == "silence":
        return np.full((t, n_ch), 512)
    if name == "dc0":
        return np.zeros((t, n_ch), int)
    if name == "dc1023":
        return np.full((t, n_ch), 1023)
    if name == "ramp":                       # slope 1..8 per channel, wraps (step of -1023)
        return (f * (c + 1)) % 1024
    if name == "ramp_small":                 # tiny details: symbols near 0
        return 400 + (f * (c + 1)) // 16
    if name == "impulse":                    # one full-scale impulse per channel
        x = np.full((t, n_ch), 512)
        for k in range(n_ch):
            x[17 + 29 * k, k] = 1023
        return x
    if name == "step":
        return np.where(f < t // 2 + 5, 100, 900) + 0 * c
    if name == "square_nyq":                 # full-scale square at Nyquist
        return np.where(f % 2 == 0, 0, 1023) + 0 * c
    if name == "square_16":
        return np.where((f // 8) % 2 == 0, 0, 1023) + 0 * c
    if name == "sine_fs":
        return np.round(511.5 + 511.5 * np.sin(2 * np.pi * f * (c + 1) / 37.0)).astype(int)
    if name == "noise_amp":                  # white noise at 8 amplitudes (one per channel)
        rng = np.random.default_rng(seed)
        a = np.array([2, 4, 8, 16, 32, 64, 128, 256])[:n_ch]
        return np.clip(512 + np.round(rng.normal(0, 1, (t, n_ch)) * a), 0, 1023).astype(int)
    if name == "lfsr":
        return stimgen.selected("lfsr", list(range(n_ch)), t)
    if name == "synthetic":
        return synthetic(n_ch, t + 5000, seed=seed)[5000:]
    if name == "real":
        _, x = load_challenge(raw, n_ch, first_file=300)
        return x[5000:5000 + t, :n_ch].astype(np.int64)
    raise ValueError(name)


def analyse(x: np.ndarray) -> dict:
    hit = {k: set() for k in CTX}
    esc = {k: 0 for k in CTX}
    peak = [0] * 4
    B = CFG.block_len
    for c in range(x.shape[1]):
        for b in range(x.shape[0] // B):
            blk = x[b * B:(b + 1) * B, c]
            for k, v in channel_symbols(blk, CFG):
                s, raw = symbol_of(v, CFG)
                hit[k].add(s)
                esc[k] += raw is not None
            for pushed in stream_coeffs(blk.astype(np.int64) - 512, CFG.levels):
                for k, _, v in pushed:
                    peak[k] = max(peak[k], abs(int(v)))
    return {"hit": hit, "esc": esc, "peak": [peak[1], peak[2], peak[3], peak[0]]}


def emit(out: Path, names, slots, n_slots: int, raw) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for i, n in enumerate(names):
        rng = np.random.default_rng(1000 + i)
        fr = rng.integers(0, 1024, size=(T, n_slots), dtype=np.int64)
        fr[:, slots] = pat(n, len(slots), raw=raw)
        np.save(out / f"{n}.npy", fr)
    (out / "config.json").write_text(json.dumps({"slots": slots, "n_slots": n_slots,
                                                 "patterns": list(names)}))
    print(f"{len(names)} stimuli ({T} frames x {n_slots} slots) in {out}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--raw", type=Path, default=ROOT / "data" / "raw",
                    help="challenge WAVs (the 'real' pattern; skipped if absent)")
    ap.add_argument("--emit", type=Path)
    ap.add_argument("--n-slots", type=int, default=32)
    ap.add_argument("--slots", default="1,5,6,12,17,22,30,31")
    a = ap.parse_args()
    names = [n for n in NAMES if n != "real" or (a.raw.exists() and any(a.raw.glob("*.wav")))]
    if a.emit:
        emit(a.emit, names, [int(s) for s in a.slots.split(",")], a.n_slots, a.raw)
        return 0
    union = {k: set() for k in CTX}
    print(f"{'pattern':11s} {'symbols hit d1/d2/d3/a3':>24s} {'escapes':>16s} "
          f"{'peak |d1| |d2| |d3| |a3|':>26s}")
    for n in names:
        r = analyse(pat(n, raw=a.raw))
        for k in CTX:
            union[k] |= r["hit"][k]
        print(f"{n:11s} {'/'.join(str(len(r['hit'][k])) for k in (1, 2, 3, 0)):>24s} "
              f"{'/'.join(str(r['esc'][k]) for k in (1, 2, 3, 0)):>16s} "
              f"{' '.join(f'{p:5d}' for p in r['peak']):>26s}")
    n_sym = CFG.n_sym
    print(f"union: {'/'.join(str(len(union[k])) for k in (1, 2, 3, 0))} of {n_sym} symbols "
          f"(d1/d2/d3/a3); row-width bounds |d1|..|a3| = {WIDTH_BOUND}")
    miss = {CTX[k]: sorted(set(range(n_sym)) - union[k]) for k in CTX}
    if any(miss.values()):
        print("never hit:", miss)
    return 0


if __name__ == "__main__":
    sys.exit(main())
