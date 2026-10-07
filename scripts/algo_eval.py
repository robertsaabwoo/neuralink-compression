"""Algorithm evaluation on the challenge data (T-ALG-2/3/5/7, model side of T-BW-1).

  python scripts/algo_eval.py [--groups N | --full] [--out result.json]

Channels are 8 challenge files stacked (one group = 8 files, like the 8 selected channels).
Files 0-31 trained the ROM tables (scripts/gen_lossy_rom.py), so they are the *training* set;
files 32+ are held out. Measures, with the shipped tables and the RTL's packet format:

  rate        bits/sample per group, overall; worst groups listed            (C-ALG-1, C-BW-1)
  quality     per-channel SNR: median, 10th percentile, worst channels       (C-ALG-2/3)
  generalise  rate on the training files vs the held-out files               (C-ALG-4)
  spikes      threshold crossings (model spike detector) on the original vs the
              reconstruction: hit rate, false positives per original spike, jitter (C-ALG-5)
  error       max |reconstruction error| (any increase is a regression)     (T-ALG-7)
  packets     bytes per packet: mean, max on real data, LFSR worst case      (C-BW-2)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "model"))

from nlc.data import load_challenge, synthetic  # noqa: E402
from nlc.lossy import LossyCodec, LossyConfig, default_tables  # noqa: E402
from nlc.packet import decode_stream, encode_stream  # noqa: E402
from nlc.spikes import FrontEndConfig, calibrate_thresholds, highpass  # noqa: E402
from nlc.stimgen import selected  # noqa: E402

N_CH = 8
TRAIN_FILES = 32
MATCH_WIN = 10            # samples (~0.5 ms) for a reconstructed spike to count as the same


def detect(x: np.ndarray, thr: np.ndarray, fe: FrontEndConfig) -> list[np.ndarray]:
    """Spike times per channel: hp <= -thr, then a refractory period (model's front end)."""
    hp = highpass(x, fe)
    out = []
    for c in range(x.shape[1]):
        idx = np.flatnonzero(hp[:, c] <= -thr[c])
        keep, last = [], -10**9
        for i in idx:
            if i - last > fe.refractory:
                keep.append(i)
                last = i
        out.append(np.asarray(keep, dtype=np.int64))
    return out


def spike_match(a: np.ndarray, b: np.ndarray) -> tuple[int, int, list[int]]:
    """(hits, false positives, |offsets|) of reconstructed spikes b against original a."""
    if len(a) == 0:
        return 0, len(b), []
    used = np.zeros(len(b), dtype=bool)
    hits, offs = 0, []
    for t in a:
        j = np.searchsorted(b, t - MATCH_WIN)
        while j < len(b) and b[j] <= t + MATCH_WIN:
            if not used[j]:
                used[j] = True
                hits += 1
                offs.append(abs(int(b[j] - t)))
                break
            j += 1
    return hits, int((~used).sum()), offs


def evaluate(x: np.ndarray, codec: LossyCodec, fe: FrontEndConfig) -> dict:
    pkts = encode_stream(x, codec)
    y = decode_stream(pkts, codec, x.shape[1])
    ref = x[:len(y)]
    err = (y - ref).astype(np.int64)
    snr = []
    for c in range(x.shape[1]):
        s = (ref[:, c] - ref[:, c].mean()).astype(float)
        snr.append(float(10 * np.log10((s ** 2).sum() / max((err[:, c].astype(float) ** 2).sum(),
                                                             1e-9))))
    thr = calibrate_thresholds(ref, fe)
    sa, sb = detect(ref, thr, fe), detect(y, thr, fe)
    n_orig = hits = fps = 0
    offs: list[int] = []
    for a, b in zip(sa, sb):
        h, f, o = spike_match(a, b)
        n_orig += len(a)
        hits += h
        fps += f
        offs += o
    return {"bits": 8 * sum(map(len, pkts)), "samples": int(ref.size), "snr": snr,
            "max_abs_err": int(np.abs(err).max()) if err.size else 0,
            "packet_bytes": [len(p) for p in pkts],
            "spikes": {"orig": n_orig, "hits": hits, "false_pos": fps, "offsets": offs}}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--groups", type=int, default=20, help="held-out groups of 8 files")
    ap.add_argument("--full", action="store_true", help="every held-out file")
    ap.add_argument("--train-groups", type=int, default=4)
    ap.add_argument("--data", default=str(ROOT / "data" / "raw"))
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()

    t0 = time.time()
    cfg = LossyConfig(n_flush=N_CH)
    codec = LossyCodec(cfg, default_tables())
    fe = FrontEndConfig()
    files = sorted(Path(args.data).glob("*.wav"))
    real = len(files) >= TRAIN_FILES + N_CH
    n_held = (len(files) - TRAIN_FILES) // N_CH if real else 0
    groups = n_held if args.full else min(args.groups, n_held)

    def load(first: int, seed: int):
        if real:
            return load_challenge(args.data, N_CH, first_file=first)[1]
        return synthetic(N_CH, 5 * 19531, seed=seed)

    held, train = [], []
    for g in range(groups if real else 4):
        r = evaluate(load(TRAIN_FILES + N_CH * g, 100 + g), codec, fe)
        r["files"] = (f"{files[TRAIN_FILES + N_CH * g].name}.." if real else f"synthetic {g}")
        held.append(r)
        print(f"held-out group {g}: {r['bits'] / r['samples']:.3f} b/sample, "
              f"median SNR {np.median(r['snr']):.1f} dB", flush=True)
    for g in range(args.train_groups if real else 0):
        train.append(evaluate(load(N_CH * g, 0), codec, fe))

    def rate(rs):
        return sum(r["bits"] for r in rs) / max(1, sum(r["samples"] for r in rs))

    snr = np.array([s for r in held for s in r["snr"]])
    sp = {k: sum(r["spikes"][k] for r in held) for k in ("orig", "hits", "false_pos")}
    offs = [o for r in held for o in r["spikes"]["offsets"]]
    pk = [b for r in held for b in r["packet_bytes"]]
    worst = codec_bytes = None
    lfsr = selected("lfsr", [3, 40, 41, 90, 128, 200, 254, 255], cfg.frames_per_packet)
    codec_bytes = len(encode_stream(lfsr, codec)[0])
    per_group = sorted(((r["bits"] / r["samples"], r["files"]) for r in held), reverse=True)
    worst = [{"files": f, "bits_per_sample": round(b, 3)} for b, f in per_group[:10]]
    res = {
        "source": "challenge" if real else "synthetic (data/raw missing)",
        "held_out_groups": len(held),
        "bits_per_sample": rate(held),
        "snr_median_db": float(np.median(snr)),
        "snr_p10_db": float(np.percentile(snr, 10)),
        "snr_min_db": float(snr.min()),
        "train_bits_per_sample": rate(train) if train else None,
        "table_generalisation_pct": (100 * (rate(held) / rate(train) - 1)) if train else None,
        "spike_hit_rate": sp["hits"] / sp["orig"] if sp["orig"] else None,
        "spike_false_pos_rate": sp["false_pos"] / sp["orig"] if sp["orig"] else None,
        "spike_jitter_samples": float(np.mean(offs)) if offs else None,
        "spikes_orig": sp["orig"],
        "recon_max_abs_err": max(r["max_abs_err"] for r in held),
        "packet_bytes_mean": float(np.mean(pk)),
        "packet_bytes_max_real": int(max(pk)),
        "packet_bytes_lfsr": codec_bytes,
        "packet_ms": cfg.frames_per_packet * 256 * 200e-6,
        "worst_groups": worst,
        "seconds": round(time.time() - t0, 1),
    }
    res["output_kbit_s_mean"] = res["packet_bytes_mean"] * 8 / res["packet_ms"]
    res["output_kbit_s_lfsr"] = codec_bytes * 8 / res["packet_ms"]
    text = json.dumps(res, indent=2)
    if args.out:
        args.out.write_text(text)
    print(text)


if __name__ == "__main__":
    main()
