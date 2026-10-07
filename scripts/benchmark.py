"""Compression ratio of every mode on the challenge data (or synthetic data).

  python scripts/benchmark.py --synthetic
  python scripts/benchmark.py --data data/raw --channels 16

Two baselines are reported, because they differ by 1.6x and are easy to mix up:
  vs 10b : raw ADC bits (what the implant would otherwise radio out)
  vs wav : 16-bit WAV samples (the compression-challenge metric)
For the spike modes, "vs feat" is against the uncompressed features (fixed-width
counts / power values per channel per window), which is the baseline the
patent's ~5:1 and ~30:1 appear to use; vs raw samples they are ~100-1000x.
The first second is used for calibration (spike thresholds, rANS tables) and
excluded from the measurement.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "model"))

from nlc import (BinnedConfig, BinnedSpikeCodec, FrontEndConfig, LosslessCodec, LossyCodec,  # noqa: E402
                 LossyConfig, RiceConfig, SbpConfig, SpikeBandPowerCodec,
                 calibrate_thresholds, decode_stream, encode_stream, stream_bits, train_tables)
from nlc.data import load_challenge, synthetic  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=Path("data/raw"))
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--channels", type=int, default=16)
    ap.add_argument("--first-file", type=int, default=0)
    args = ap.parse_args()

    if args.synthetic:
        fs, x = 19531, synthetic(args.channels, 5 * 19531, seed=0)
    else:
        fs, x = load_challenge(args.data, args.channels, args.first_file)
    cal, x = x[:fs], x[fs:]
    n_ch = x.shape[1]
    print(f"{n_ch} channels, {x.shape[0] / fs:.2f} s measured at {fs} Hz "
          f"({'synthetic' if args.synthetic else args.data})\n")

    fe = FrontEndConfig()
    thr = calibrate_thresholds(cal, fe)
    lossy_cfg = LossyConfig()
    codecs = {
        "0 lossless (delta+Rice)": LosslessCodec(RiceConfig()),
        "1 lossy (5/3 DWT+rANS)": LossyCodec(lossy_cfg, train_tables(cal, lossy_cfg)),
        "2 binned spikes": BinnedSpikeCodec(fe, BinnedConfig(), thr),
        "3 spike-band power": SpikeBandPowerCodec(fe, SbpConfig()),
    }

    feat_bits = {2: (BinnedConfig().c_max).bit_length(), 3: SbpConfig().sbp_bits}
    print(f"{'mode':26s} {'bits/sample':>11s} {'vs 10b':>8s} {'vs wav':>8s} {'vs feat':>8s}  notes")
    for name, codec in codecs.items():
        pkts = encode_stream(x, codec)
        n = len(pkts) * codec.frames_per_packet * n_ch
        bps = stream_bits(pkts) / n
        y = decode_stream(pkts, codec, n_ch)
        ref = x[: len(pkts) * codec.frames_per_packet]
        if codec.mode == 0:
            note = "bit-exact" if np.array_equal(y, ref) else "MISMATCH"
        elif codec.mode == 1:
            err = (y - ref).astype(float)
            sig = (ref - ref.mean(axis=0)).astype(float)
            note = f"SNR {10 * np.log10((sig ** 2).sum() / (err ** 2).sum()):.1f} dB"
        elif codec.mode == 2:
            note = f"{y.sum() / (ref.shape[0] / fs) / n_ch:.1f} spikes/s/ch"
        else:
            note = f"{(y > 0).mean() * 100:.0f}% channels non-zero"
        feat = (f"{len(pkts) * n_ch * feat_bits[codec.mode] / stream_bits(pkts):8.2f}"
                if codec.mode in feat_bits else f"{'-':>8s}")
        print(f"{name:26s} {bps:11.3f} {10 / bps:8.2f} {16 / bps:8.2f} {feat}  {note}")


if __name__ == "__main__":
    main()
