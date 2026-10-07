"""Export golden-model test vectors for the cocotb testbench.

  python scripts/gen_vectors.py --out test/vectors

Per mode, in <out>/<mode>/:
  input.hex     one 10-bit sample per line, frame-major over the selected channels
  expected.hex  one output word per line: bit 8 = last byte of packet, bits 7:0 = byte;
                a final 0x200 terminator marks the end (used by the replay mock)
  config.json   codec config, selected ADC slots, frame length, packet sizes

Channels are placed at `--slots` inside an `--n-slots` ADC frame. The real mux
has 256 slots; a short frame keeps simulation fast and exercises the same logic.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "model"))

from nlc import (BinnedConfig, BinnedSpikeCodec, FrontEndConfig, LosslessCodec,  # noqa: E402
                 RiceConfig, SbpConfig, SpikeBandPowerCodec, calibrate_thresholds,
                 encode_stream)
from nlc.data import synthetic  # noqa: E402
from nlc.lossy import LossyCodec, LossyConfig, default_tables, fifo_profile  # noqa: E402

MAX_WORDS = 8192  # replay mock memory depth


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slots", type=int, nargs="+", default=[1, 4, 6, 11])
    ap.add_argument("--n-slots", type=int, default=32)   # x 2 clocks = 64-clock frames (C-IF-9)
    ap.add_argument("--frames", type=int, default=600)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", type=Path, default=Path("test/vectors"))
    args = ap.parse_args()

    k = len(args.slots)
    x = synthetic(k, args.frames, seed=args.seed, rate_hz=100.0)
    fe = FrontEndConfig()
    thr = np.minimum(calibrate_thresholds(x, fe), 255)
    rice = RiceConfig(frames_per_packet=32)
    binned = BinnedConfig(bin_len=200, group=4)
    sbp = SbpConfig(sbp_len=200, sbp_shift=5)
    lossy = LossyConfig(n_flush=8)            # the RTL flushes all N_SEL = 8 channel states
    cases = {
        "lossless": (LosslessCodec(rice), {"rice": asdict(rice)}),
        "binned": (BinnedSpikeCodec(fe, binned, thr),
                   {"frontend": asdict(fe), "binned": asdict(binned), "thresholds": thr.tolist()}),
        "sbp": (SpikeBandPowerCodec(fe, sbp), {"frontend": asdict(fe), "sbp": asdict(sbp)}),
        "lossy": (LossyCodec(lossy, default_tables()), {"lossy": asdict(lossy)}),
    }
    for name, (codec, cfg) in cases.items():
        d = args.out / name
        d.mkdir(parents=True, exist_ok=True)
        pkts = encode_stream(x, codec)
        n_frames = len(pkts) * codec.frames_per_packet
        words = [(int(i == len(p) - 1) << 8) | b for p in pkts for i, b in enumerate(p)]
        if len(words) >= MAX_WORDS:
            raise ValueError(f"{name}: {len(words)} bytes exceeds replay depth {MAX_WORDS}")
        # lossy: a packet's last symbols leave the FIFO `lag` frames after its last frame
        n_in = n_frames + (fifo_profile(lossy)[1] if name == "lossy" else 0)
        (d / "input.hex").write_text("".join(f"{v:03x}\n" for v in x[:n_in].ravel()))
        (d / "expected.hex").write_text("".join(f"{w:03x}\n" for w in words) + "200\n")
        cfg.update(mode=codec.mode, slots=args.slots, n_slots=args.n_slots, frames=n_frames,
                   packets=len(pkts), packet_bytes=[len(p) for p in pkts])
        (d / "config.json").write_text(json.dumps(cfg, indent=2))
        print(f"{name:9s} {n_frames} frames -> {len(pkts)} packets, {len(words)} bytes")


if __name__ == "__main__":
    main()
