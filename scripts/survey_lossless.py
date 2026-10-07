"""Lossless candidates on the full challenge dataset, measured the challenge way
(total WAV bytes / total compressed bytes).

  python scripts/survey_lossless.py [--data data/raw] [--files N]

All files run side by side as channels of one TDM stream (one time step at a
time, vectorised over channels), which is how the hardware sees them. Every
variant is integer-only and causal. Code lengths are exact for Rice; for the
static rANS variants they are ideal code lengths -log2(f/4096) with the
quantised 12-bit tables (the coder itself adds < 0.1% plus a 4-byte flush).

Predictors
  delta : e = x[n] - x[n-1]                                    (patent lossless)
  lms8  : delta, then an 8-tap sign-sign LMS on past deltas    (hxrdxkxvxd's
          weights/steps, integer: pred = sum(w*d) >> 12, e = d + pred)
Residuals are wrapped to 10 bits (x is known to be in [0, 1023]).

Coders
  rice      : adaptive-k Rice (JPEG-LS A/N tracker), escape after 16   (patent lossless)
  rice-ctx4 : same, one A/N tracker per context
  rans-ctx4 : static 64-symbol tables per context (63 = escape + 10 raw bits),
              trained on the other half of the files                   (patent's static rANS)
Contexts (hxr's rule, integer): |previous residual| against 1x/3x/5x a slow
average of |residual| (acc += |e| - acc >> 17).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "model"))

from nlc.data import read_wav, wav_to_adc  # noqa: E402
from nlc.rans import normalize_freqs  # noqa: E402

LMS_W0 = np.array([700, 400, 500, 400, 400, 100, 250, 50])
LMS_STEP = np.array([6, 4, 3, 2, 2, 1, 1, 1])
Q_LIMIT, N_RESET, A_INIT, K_MAX, ESC_RAW = 16, 64, 16, 9, 10


def load(data_dir: Path, n_files: int | None):
    files = sorted(data_dir.glob("*.wav"))[:n_files]
    chans, wav_bytes = [], 0
    for f in files:
        _, x16 = read_wav(f)
        chans.append(wav_to_adc(x16)[0])
        wav_bytes += f.stat().st_size
    lens = np.array([len(c) for c in chans])
    x = np.zeros((lens.max(), len(chans)), dtype=np.int64)
    for i, c in enumerate(chans):
        x[: len(c), i] = c
    return x, lens, wav_bytes


def residuals(x: np.ndarray, predictor: str):
    """Wrapped residual e[t, ch] and context[t, ch] for t >= 1."""
    T, C = x.shape
    e = np.zeros((T, C), dtype=np.int64)
    ctx = np.zeros((T, C), dtype=np.int64)
    hist = np.zeros((8, C), dtype=np.int64)
    w = np.tile(LMS_W0[:, None], (1, C))
    acc = np.full(C, 30 << 17, dtype=np.int64)
    prev_mag = np.zeros(C, dtype=np.int64)
    for t in range(1, T):
        d = x[t] - x[t - 1]
        if predictor == "lms8":
            pred = (hist * w).sum(axis=0) >> 12
            r = d + pred
            sgn = np.where(r > 0, 1, -1)
            w -= np.sign(hist) * sgn * LMS_STEP[:, None]
            np.clip(w, -8192, 8192, out=w)
            hist[1:] = hist[:-1]
            hist[0] = d
        else:
            r = d
        rw = ((r + 512) & 1023) - 512
        avg = np.maximum(1, acc >> 17)
        ctx[t] = ((prev_mag >= avg).astype(np.int64) + (prev_mag >= 3 * avg) + (prev_mag >= 5 * avg))
        e[t] = rw
        mag = np.abs(rw)
        acc += mag - (acc >> 17)
        prev_mag = mag
    return e, ctx


def rice_bits(e: np.ndarray, ctx: np.ndarray, lens: np.ndarray, n_ctx: int,
              reset_every: int | None = None) -> np.ndarray:
    """Per-channel Rice bits (first sample raw 10 bits), JPEG-LS k per context."""
    T, C = e.shape
    cols = np.arange(C)
    A = np.full((n_ctx, C), A_INIT, dtype=np.int64)
    N = np.ones((n_ctx, C), dtype=np.int64)
    bits = np.full(C, 10, dtype=np.int64)
    for t in range(1, T):
        live = t < lens
        if reset_every and t % reset_every == 0:
            A[:] = A_INIT
            N[:] = 1
            bits += 10 * live
            continue
        k_ctx = ctx[t] if n_ctx > 1 else np.zeros(C, dtype=np.int64)
        a, n = A[k_ctx, cols], N[k_ctx, cols]
        k = np.zeros(C, dtype=np.int64)
        for _ in range(K_MAX):
            k += (n << k) < a
        m = np.where(e[t] >= 0, 2 * e[t], -2 * e[t] - 1)
        q = m >> k
        b = np.where(q < Q_LIMIT, q + 1 + k, Q_LIMIT + ESC_RAW)
        bits += b * live
        a = a + np.abs(e[t])
        n = n + 1
        half = n == N_RESET
        A[k_ctx, cols] = np.where(half, a >> 1, a)
        N[k_ctx, cols] = np.where(half, n >> 1, n)
    return bits


def rans_ctx_bits(e, ctx, lens, train_cols, test_cols, n_ctx=4):
    """Ideal code length with static 12-bit tables trained on train_cols."""
    m = np.where(e >= 0, 2 * e, -2 * e - 1)
    sym = np.minimum(m, 63)
    T = e.shape[0]
    valid = (np.arange(T)[:, None] < lens[None, :]) & (np.arange(T)[:, None] >= 1)
    tables = []
    for k in range(n_ctx):
        sel = valid[:, train_cols] & (ctx[:, train_cols] == k)
        counts = np.bincount(sym[:, train_cols][sel], minlength=64)
        tables.append(np.array(normalize_freqs(counts, 4096)))
    cost = np.zeros(e.shape, dtype=np.float64)
    for k in range(n_ctx):
        sel = ctx == k
        cost[sel] = -np.log2(tables[k][sym[sel]] / 4096.0)
    cost += (sym == 63) * ESC_RAW
    cost[~valid] = 0
    return cost[:, test_cols].sum(axis=0) + 10 + 32          # raw first sample + flush


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=Path("data/raw"))
    ap.add_argument("--files", type=int, default=None)
    args = ap.parse_args()

    x, lens, wav_bytes = load(args.data, args.files)
    C = x.shape[1]
    print(f"{C} files, {lens.sum()} samples, {wav_bytes} WAV bytes\n")

    def report(name, bits_per_file, patent):
        comp = int(np.ceil(bits_per_file / 8).sum()) + 8 * C          # + small header per file
        print(f"{name:44s} {8 * comp / lens.sum():6.3f} b/sample  "
              f"ratio {wav_bytes / comp:6.4f}  (vs 10-bit {10 * lens.sum() / (8 * comp):5.3f})  {patent}")

    print(f"{'reference (C, measured here)':44s}")
    print(f"{'  brainwire: delta + EMA-k Rice':44s} {'':16s}ratio 3.3552                       patent lossless")
    print(f"{'  hxrdxkxvxd: LMS8 + 4ctx adaptive range coder':44s} {'':16s}ratio 3.4631                       no (per-symbol adaptive range coder)\n")

    half_a, half_b = np.arange(0, C, 2), np.arange(1, C, 2)
    for pred in ("delta", "lms8"):
        e, ctx = residuals(x, pred)
        report(f"{pred} + rice", rice_bits(e, ctx, lens, 1), "patent lossless" if pred == "delta" else "patent lossless + predictor")
        if pred == "delta":
            report("delta + rice, reset every 128 samples", rice_bits(e, ctx, lens, 1, 128), "mode 0 as packetised now")
            report("delta + rice, reset every 2048 samples", rice_bits(e, ctx, lens, 1, 2048), "")
        report(f"{pred} + rice-ctx4", rice_bits(e, ctx, lens, 4), "Rice, context-selected k")
        bits = np.zeros(C)
        bits[half_b] = rans_ctx_bits(e, ctx, lens, half_a, half_b)
        bits[half_a] = rans_ctx_bits(e, ctx, lens, half_b, half_a)
        report(f"{pred} + rans-ctx4 (static, 2-fold)", bits, "patent static-codebook rANS")
        print()


if __name__ == "__main__":
    main()
