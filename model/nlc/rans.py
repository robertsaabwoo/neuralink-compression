"""Byte-wise rANS with static, multi-context frequency tables (after F. Giesen's
rans_byte.h). 32-bit state, L = 2^23, PROB_BITS = 12.

Hardware-oriented ordering: ANS is LIFO, and the usual software convention makes
the *encoder* run backwards over a buffered block. Here the encoder (on chip)
runs forwards and streams renormalisation bytes out as it goes; the decoder
(off chip, memory is free) starts from the final state and walks the stream
backwards, recovering symbols last-first. No symbol buffer on the implant.

Stream layout: renorm bytes in emission order, then the 4-byte little-endian
final state. The decoder reads the state from the end and consumes bytes
backwards.

Hardware cost (plan step 7): the encoder needs x // freq and x % freq per symbol
(a serial divider, or a reciprocal multiply plus a freq ROM), and a ROM of
n_ctx * n_sym * 2 * PROB_BITS bits for freq/cum. The slot table is decoder-only.
"""

from __future__ import annotations

import numpy as np

PROB_BITS = 12
PROB_SCALE = 1 << PROB_BITS
RANS_L = 1 << 23


def normalize_freqs(counts, total: int = PROB_SCALE) -> list[int]:
    """Scale histogram counts to sum exactly `total`, every symbol >= 1."""
    counts = np.asarray(counts, dtype=np.float64) + 0.5  # keep every symbol codable
    freqs = np.maximum(1, np.floor(counts / counts.sum() * total)).astype(np.int64)
    while freqs.sum() != total:
        i = int(np.argmax(freqs))
        freqs[i] += 1 if freqs.sum() < total else -1
    return [int(f) for f in freqs]


class RansTables:
    def __init__(self, freqs: list[list[int]]) -> None:
        self.freqs = freqs
        self.cum = []
        self.slot = []
        for f in freqs:
            if sum(f) != PROB_SCALE or min(f) < 1:
                raise ValueError("each table must sum to PROB_SCALE with all freqs >= 1")
            cum = [0]
            for v in f:
                cum.append(cum[-1] + v)
            self.cum.append(cum)
            slot = np.empty(PROB_SCALE, dtype=np.int64)
            for s, v in enumerate(f):
                slot[cum[s]:cum[s] + v] = s
            self.slot.append(slot)


def rans_encode(symbols: list[tuple[int, int]], tables: RansTables) -> bytes:
    """symbols: (context, symbol) pairs, encoded in the given (forward) order."""
    x = RANS_L
    out = bytearray()
    for ctx, s in symbols:
        freq = tables.freqs[ctx][s]
        x_max = ((RANS_L >> PROB_BITS) << 8) * freq
        while x >= x_max:
            out.append(x & 0xFF)
            x >>= 8
        x = ((x // freq) << PROB_BITS) + (x % freq) + tables.cum[ctx][s]
    out += x.to_bytes(4, "little")
    return bytes(out)


def rans_decode(data: bytes, tables: RansTables, ctxs: list[int]) -> list[int]:
    """Decode len(ctxs) symbols; ctxs and the result are in forward order."""
    mask = PROB_SCALE - 1
    x = int.from_bytes(data[-4:], "little")
    pos = len(data) - 4
    out = []
    for ctx in reversed(ctxs):
        s = int(tables.slot[ctx][x & mask])
        x = tables.freqs[ctx][s] * (x >> PROB_BITS) + (x & mask) - tables.cum[ctx][s]
        while x < RANS_L:
            pos -= 1
            x = (x << 8) | data[pos]
        out.append(s)
    if pos != 0 or x != RANS_L:
        raise ValueError("rANS stream not fully consumed / corrupt")
    out.reverse()
    return out
