"""Static (offline-trained) vs packet-adaptive rANS tables on the lossy-mode symbols.

  python scripts/compare_rans_tables.py --data data/raw      # challenge data
  python scripts/compare_rans_tables.py --synthetic          # pipeline check only

Symbols come from the lossy front end (model/nlc/lossy.py: 5/3 lifting DWT +
quantisation): per channel and 64-frame block, 8 approximation deltas and
8/16/32 detail coefficients (context = subband). Values in [-31, 31] map to
symbols 0..62, anything else to ESC (63) plus 16 raw bits, so all methods code
the same symbols and the reconstruction (SNR) is identical. One packet = N
blocks of all channels, issued in TDM order: coefficient j of channel 0..C-1.

The first second trains the static tables; everything after it is measured.
Every method is run through the bit-exact golden encoders in nlc.rans_tdm,
so the byte counts are what the RTL produces.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "model"))

from nlc.data import load_challenge, synthetic  # noqa: E402
from nlc.lossy import LossyConfig, _channel_symbols  # noqa: E402
from nlc.rans_tdm import (TdmRansConfig, TdmRansEncoder, TdmRansStaticEncoder,  # noqa: E402
                          build_static_cdf)

ESC, VMAX, ESC_BITS = 63, 31, 16


def packets(x: np.ndarray, lcfg: LossyConfig, blocks_per_packet: int):
    """Yield (chans, ctxs, symbols, n_escapes) per packet, TDM order."""
    B, C = lcfg.block_len, x.shape[1]
    n_blocks = x.shape[0] // B
    for p0 in range(0, n_blocks - blocks_per_packet + 1, blocks_per_packet):
        chans, ctxs, syms, n_esc = [], [], [], 0
        for b in range(p0, p0 + blocks_per_packet):
            per_ch = [_channel_symbols(x[b * B:(b + 1) * B, c], lcfg) for c in range(C)]
            for j in range(len(per_ch[0])):
                for c in range(C):
                    k, v = per_ch[c][j]
                    s = v + VMAX if abs(v) <= VMAX else ESC
                    n_esc += s == ESC
                    chans.append(c)
                    ctxs.append(k)
                    syms.append(s)
        yield chans, ctxs, syms, n_esc


def entropy_bits(counts: np.ndarray) -> float:
    p = counts[counts > 0] / counts.sum()
    return float(-(counts[counts > 0] * np.log2(p)).sum())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=Path("data/raw"))
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--seconds", type=float, default=4.0, help="measured duration after training")
    ap.add_argument("--blocks", type=int, nargs="+", default=[1, 2], help="64-frame blocks per packet")
    args = ap.parse_args()

    rcfg = TdmRansConfig()
    lcfg = LossyConfig()
    fs = 19531
    if args.synthetic:
        x = synthetic(rcfg.n_ch, int((1 + args.seconds) * fs), seed=0)
        src = "SYNTHETIC (pipeline check only, not a result)"
    else:
        fs, x = load_challenge(args.data, rcfg.n_ch)
        x = x[: int((1 + args.seconds) * fs)]
        src = str(args.data)
    train, test = x[:fs], x[fs:]
    n_samples = (test.shape[0] // lcfg.block_len) * lcfg.block_len * test.shape[1]
    print(f"{src}: {rcfg.n_ch} channels, train 1.0 s, test {test.shape[0] / fs:.2f} s\n")

    # offline training: per-context and pooled histograms
    hist = np.zeros((rcfg.n_ctx, rcfg.n_sym), dtype=np.int64)
    for _, ctxs, syms, _ in packets(train, lcfg, 1):
        np.add.at(hist, (np.array(ctxs), np.array(syms)), 1)
    per_ctx = [build_static_cdf(h, rcfg) for h in hist]
    pooled = build_static_cdf(hist.sum(axis=0), rcfg)

    print(f"{'method':34s} {'pkt':>3s} {'rANS':>7s} {'escape':>7s} {'total':>7s} "
          f"{'ratio':>6s} {'vs bound':>9s}   (bits per sample)")
    for bpp in args.blocks:
        pk = list(packets(test, lcfg, bpp))
        esc_bits = sum(p[3] for p in pk) * ESC_BITS

        test_hist = np.zeros((rcfg.n_ctx, rcfg.n_sym), dtype=np.int64)
        for _, ctxs, syms, _ in pk:
            np.add.at(test_hist, (np.array(ctxs), np.array(syms)), 1)

        results = {}
        enc = TdmRansStaticEncoder(rcfg, [pooled] * rcfg.n_ctx)
        results["static, 1 table"] = sum(8 * len(enc.encode_packet(ch, sy, k)) for ch, k, sy, _ in pk)
        enc = TdmRansStaticEncoder(rcfg, per_ctx)
        results["static, 4 tables (per subband)"] = sum(
            8 * len(enc.encode_packet(ch, sy, k)) for ch, k, sy, _ in pk)
        enc = TdmRansEncoder(rcfg)
        results["adaptive per packet, 1 table"] = sum(8 * len(enc.encode_packet(ch, sy)) for ch, _, sy, _ in pk)

        bound = sum(entropy_bits(h) for h in test_hist) + esc_bits
        for name, bits in results.items():
            total = bits + esc_bits
            print(f"{name:34s} {bpp:3d} {bits / n_samples:7.3f} {esc_bits / n_samples:7.3f} "
                  f"{total / n_samples:7.3f} {10 * n_samples / total:6.2f} "
                  f"{100 * (total / bound - 1):+8.1f}%")
        print(f"{'  bound (see below)':34s} {bpp:3d} {(bound - esc_bits) / n_samples:7.3f} "
              f"{esc_bits / n_samples:7.3f} {bound / n_samples:7.3f} {10 * n_samples / bound:6.2f}\n")

    print("rANS includes the per-packet flush of every channel's final state (4 bytes each).")
    print("escape = 16 raw bits per coefficient outside [-31, 31]; ratio vs 10-bit raw.")
    print("bound = per-subband entropy of the test symbols: an ideal coder with static tables")
    print("        trained on the test data itself and no flush overhead.")


if __name__ == "__main__":
    main()
