"""Export golden-model test vectors for the cocotb testbench (lossy mode: the only
mode in silicon, decision D1).

  python scripts/gen_vectors.py --out test/vectors

In <out>/lossy/:
  input.hex     one 10-bit sample per line, frame-major over the selected channels
  expected.hex  one output word per line: bit 8 = last byte of packet, bits 7:0 = byte;
                a final 0x200 terminator marks the end (used by the replay mock)
  config.json   codec config, selected ADC slots, frame length, packet sizes

Channels are placed at `--slots` inside an `--n-slots` ADC frame. The real mux
has 256 slots; a short frame keeps simulation fast and exercises the same logic.

Post-layout power vectors (T-PWR-3, scripts/flow/flow.py step layout): real data at the
operating frame rate through the pins, 128 slots x 2 clocks = 256 clocks per frame:
  python scripts/gen_vectors.py --name power --source real --n-slots 128 --frames 512 \
      --slots 1 20 21 45 64 100 126 127
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

import numpy as np


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "model"))

from nlc import encode_stream  # noqa: E402
from nlc.data import load_challenge, synthetic  # noqa: E402
from nlc.lossy import LossyCodec, LossyConfig, default_tables, fifo_profile  # noqa: E402

MAX_WORDS = 8192  # replay mock memory depth
ROOT = Path(__file__).resolve().parents[1]


def real(k: int, n: int, offset: int = 5000) -> np.ndarray:
    """Challenge files 300+ (not in the ROM training set), as test/env/nlc_env.py `real`."""
    _, x = load_challenge(ROOT / "data" / "raw", k, first_file=300)
    return x[offset:offset + n, :k].astype(np.int64)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slots", type=int, nargs="+", default=[1, 4, 6, 11])
    ap.add_argument("--n-slots", type=int, default=32)   # x 2 clocks = 64-clock frames (C-IF-9)
    ap.add_argument("--frames", type=int, default=600)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", type=Path, default=Path("test/vectors"))
    ap.add_argument("--name", default="lossy", help="vector set: <out>/<name>/")
    ap.add_argument("--source", choices=["synthetic", "real"], default="synthetic",
                    help="real: data/raw (scripts/fetch_data.py), synthetic when absent")
    args = ap.parse_args()

    k = len(args.slots)
    lossy = LossyConfig(n_flush=8)            # the RTL flushes all N_SEL = 8 channel states
    n_raw = args.frames + fifo_profile(lossy)[1]          # real: packets + the lag frames below
    source = args.source
    if source == "real" and not (ROOT / "data" / "raw").exists():
        print("data/raw missing (scripts/fetch_data.py): synthetic data instead")
        source = "synthetic"
    x = (real(k, n_raw) if source == "real"
         else synthetic(k, args.frames, seed=args.seed, rate_hz=100.0))
    cases = {args.name: (LossyCodec(lossy, default_tables()), {"lossy": asdict(lossy)})}
    for name, (codec, cfg) in cases.items():
        d = args.out / name
        d.mkdir(parents=True, exist_ok=True)
        pkts = encode_stream(x[:args.frames], codec)
        n_frames = len(pkts) * codec.frames_per_packet
        words = [(int(i == len(p) - 1) << 8) | b for p in pkts for i, b in enumerate(p)]
        if len(words) >= MAX_WORDS:
            raise ValueError(f"{name}: {len(words)} bytes exceeds replay depth {MAX_WORDS}")
        # lossy: a packet's last symbols leave the FIFO `lag` frames after its last frame
        n_in = n_frames + fifo_profile(lossy)[1]
        (d / "input.hex").write_text("".join(f"{v:03x}\n" for v in x[:n_in].ravel()))
        (d / "expected.hex").write_text("".join(f"{w:03x}\n" for w in words) + "200\n")
        cfg.update(mode=codec.mode, source=source, slots=args.slots, n_slots=args.n_slots,
                   frames=n_frames,
                   packets=len(pkts), packet_bytes=[len(p) for p in pkts])
        (d / "config.json").write_text(json.dumps(cfg, indent=2))
        print(f"{name:9s} {n_frames} frames -> {len(pkts)} packets, {len(words)} bytes")


if __name__ == "__main__":
    main()
