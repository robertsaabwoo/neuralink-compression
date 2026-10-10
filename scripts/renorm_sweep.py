"""docs/results.md F21.  python scripts/renorm_sweep.py [groups=6] [seconds=2.0]

Compression vs rANS renormalisation unit (b bits) and lsh, on held-out real data.

Uses the model's own symbol / context / escape streams (LossyCodec.encode_payload logic) and
shipped tables; only the renormalisation unit and lsh change. Every packet is decoded back
and compared (round trip). Bits are counted exactly: header 8, renorm emissions, raw escape
bytes, n_flush final states of (prob_bits + lsh + b) bits, packet padded to whole bytes
(an output packer). Divider steps = lsh + b.
"""
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "model"))
from nlc.data import load_challenge                      # noqa: E402
from nlc.lossy import (LossyCodec, LossyConfig, channel_symbols, coding_order,  # noqa: E402
                       default_tables, symbol_of)

TRAIN_FILES, N_CH = 32, 8


def packet_streams(codec, block):
    cfg, n_ch, B = codec.cfg, block.shape[1], codec.cfg.block_len
    per_ch = [[kv for b in range(cfg.blocks_per_packet)
               for kv in channel_symbols(block[b * B:(b + 1) * B, c], cfg)] for c in range(n_ch)]
    out = []
    for c, j in coding_order(cfg, n_ch):
        k, v = per_ch[c][j]
        s, raw = symbol_of(v, cfg)
        out.append((c, k, s, raw))
    return out


def encode(stream, tables, lsh, b, n_flush, pb):
    m = 1 << pb
    L = m << lsh
    st, chunks = {}, []                       # chunks: (value, width), in emit order
    for ch, k, s, raw in stream:
        if raw is not None:
            for byte in raw:
                chunks.append((byte, 8))
        cdf = tables[k]
        f, c = cdf[s + 1] - cdf[s], cdf[s]
        x = st.get(ch, L)
        while x >= f << (lsh + b):
            chunks.append((x & ((1 << b) - 1), b))
            x >>= b
        st[ch] = (x // f) * m + (x % f) + c
    fb = pb + lsh + b
    for ch in range(n_flush):
        chunks.append((st.get(ch, L), fb))
    return chunks


def decode(chunks, stream, tables, lsh, b, n_flush, pb, esc, esc_bytes):
    m = 1 << pb
    L = m << lsh
    fb = pb + lsh + b
    pos = len(chunks) - n_flush
    st = {ch: chunks[pos + ch][0] for ch in range(n_flush)}
    out = [None] * len(stream)
    for i in range(len(stream) - 1, -1, -1):
        ch, k, _, _ = stream[i]
        cdf = tables[k]
        x = st[ch]
        slot = x % m
        s = next(t for t in range(len(cdf) - 1) if cdf[t] <= slot < cdf[t + 1])
        f, c = cdf[s + 1] - cdf[s], cdf[s]
        x = f * (x // m) + slot - c
        while x < L:
            pos -= 1
            x = (x << b) | chunks[pos][0]
        if s == esc:
            pos -= esc_bytes
        st[ch] = x
        out[i] = s
    return out


def main():
    cfg = LossyConfig(n_flush=8)
    codec = LossyCodec(cfg, default_tables())
    tables, pb = codec.tables, cfg.prob_bits
    files = sorted((ROOT / "data" / "raw").glob("*.wav"))
    groups = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 2.0
    fpp = cfg.frames_per_packet
    streams, n_samples = [], 0
    for g in range(groups):
        x = load_challenge(ROOT / "data" / "raw", N_CH, first_file=TRAIN_FILES + N_CH * g)[1]
        n = int(secs * 19531) // fpp * fpp
        for t0 in range(0, n, fpp):
            streams.append(packet_streams(codec, x[t0:t0 + fpp]))
        n_samples += n * N_CH
    combos = [(8, 2), (8, 1), (4, 2), (4, 3), (4, 1), (2, 2), (2, 3), (1, 2), (1, 3), (8, 11)]
    print(f"{groups} held-out groups x {secs} s, {len(streams)} packets, {n_samples} samples")
    print(f"{'b':>2} {'lsh':>3} {'div steps':>9} {'bits/sample':>11} {'vs today':>8} {'flush bits/pkt':>14}  check")
    base = None
    for b, lsh in combos:
        bits, ok = 0, True
        for i, stream in enumerate(streams):
            ch = encode(stream, tables, lsh, b, cfg.n_flush, pb)
            nbits = 8 + sum(w for _, w in ch)
            bits += -(-nbits // 8) * 8                          # packer: whole bytes per packet
            if i < 3:                                           # round trip on a few packets
                dec = decode(ch, stream, tables, lsh, b, cfg.n_flush, pb, cfg.esc, cfg.esc_bytes)
                ok &= dec == [s for _, _, s, _ in stream]
        bps = bits / n_samples
        base = base or bps
        print(f"{b:>2} {lsh:>3} {lsh + b:>9} {bps:11.4f} {100 * (bps / base - 1):+7.2f}% "
              f"{cfg.n_flush * (pb + lsh + b):>14}  {'round trip ok' if ok else 'ROUND TRIP FAILED'}")


if __name__ == "__main__":
    main()
