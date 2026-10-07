"""Channel-interleaved rANS for an II=1 TDM pipeline: golden model for
src/robs_rANS/rans_tdm_adaptive.sv and src/robs_rANS/rans_tdm_static.sv.

Shared coding core (both variants)
----------------------------------
Encoding runs forwards (on chip, no symbol buffer); decoding runs backwards
(off chip). One rANS state per channel; every table in use has the same total
M, and L = M << lsh, so M divides L and the coder is exactly invertible even
for a non power-of-two M. States live in [L, 2^8 L); the renormalisation
bound is a pure shift:

  x_max(s) = (L / M) * 2^8 * f_s = f_s << (lsh + 8)

Per symbol (channel ch, context k, symbol s), with table cdf_k:
  x = state[ch], or L if ch has not been used yet in this packet
  while x >= f_s << (lsh + 8): emit x & 0xFF; x >>= 8      (at most 2 bytes)
  x = (x // f_s) * M + (x % f_s) + c_s

Bytes go out in symbol order, so all channels share one stream. At the end of
a packet every channel's final state is emitted, channel 0..n_ch-1,
state_bytes bytes little-endian each (states are below 2^(prob_bits + lsh + 8),
so a static coder needs ceil((prob_bits + lsh + 8) / 8) bytes). The decoder needs the channel and context sequences
(both fixed by the TDM schedule and the wavelet structure).

Raw bytes (escapes): a symbol may carry raw bytes, emitted *before* its
renormalisation bytes. Walking backwards, the decoder reads the renorm bytes,
then sees the decoded symbol is an escape and reads the raw bytes, so no
length field or side buffer is needed. Which symbol carries raw bytes, and how
many, is the caller's convention (lossy.py: the ESC symbol, esc_bytes bytes).

Adaptive variant (TdmRansEncoder / TdmRansDecoder)
--------------------------------------------------
One table, which may only change between packets (otherwise the backwards
decoder could not know it):
  packet 0:    f_s = 1 for every symbol
  packet p+1:  f_s = 1 + (count of s in packet p); counting stops once
               m_max - n_sym symbols have been counted, so M <= m_max
  The decoder rebuilds the same table after decoding packet p.

Static variant (TdmRansStaticEncoder / TdmRansStaticDecoder)
------------------------------------------------------------
n_ctx fixed tables (e.g. one per wavelet subband) loaded before encoding,
each summing to exactly M = 2^prob_bits, so q * M is a shift. Tables are
trained offline with build_static_cdf.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass

from .rans import normalize_freqs


@dataclass(frozen=True)
class TdmRansConfig:
    n_sym: int = 64
    n_ch: int = 32
    lsh: int = 11          # L = M << lsh; quotient width is lsh + 8 bits
    m_max: int = 4096      # adaptive: largest table total
    prob_bits: int = 12    # static: every table sums to 2^prob_bits
    n_ctx: int = 4         # static: number of tables
    state_bytes: int = 4   # bytes per channel in the end-of-packet state flush

    @property
    def count_cap(self) -> int:
        return self.m_max - self.n_sym


# ---------------------------------------------------------------------------
# shared core
# ---------------------------------------------------------------------------

def encode_core(chans, symbols, ctxs, cdfs, cfg: TdmRansConfig, raws=None) -> bytes:
    """raws: optional per-symbol bytes (or None), emitted before that symbol's renorm bytes."""
    m = cdfs[0][-1]
    if any(c[-1] != m for c in cdfs):
        raise ValueError("all tables in a packet must share the same total M")
    L = m << cfg.lsh
    states: dict[int, int] = {}
    out = bytearray()
    for i, (ch, k, s) in enumerate(zip(chans, ctxs, symbols)):
        if raws is not None and raws[i] is not None:
            out += raws[i]
        cdf = cdfs[k]
        f, c = cdf[s + 1] - cdf[s], cdf[s]
        x = states.get(ch, L)
        while x >= f << (cfg.lsh + 8):
            out.append(x & 0xFF)
            x >>= 8
        states[ch] = (x // f) * m + (x % f) + c
    for ch in range(cfg.n_ch):
        out += states.get(ch, L).to_bytes(cfg.state_bytes, "little")
    return bytes(out)


def decode_core(data, chans, ctxs, cdfs, cfg: TdmRansConfig) -> list[int]:
    return decode_core_raw(data, chans, ctxs, cdfs, cfg)[0]


def decode_core_raw(data, chans, ctxs, cdfs, cfg: TdmRansConfig, esc_sym=None,
                    esc_bytes=0) -> tuple[list[int], list[bytes | None]]:
    """As decode_core; symbols equal to esc_sym also return their esc_bytes raw bytes."""
    m = cdfs[0][-1]
    L = m << cfg.lsh
    sb = cfg.state_bytes
    pos = len(data) - sb * cfg.n_ch
    if pos < 0:
        raise ValueError("corrupt packet: shorter than the state flush")
    states = [int.from_bytes(data[pos + sb * c: pos + sb * c + sb], "little")
              for c in range(cfg.n_ch)]
    out = [0] * len(chans)
    raws: list[bytes | None] = [None] * len(chans)
    for i in reversed(range(len(chans))):
        ch, cdf = chans[i], cdfs[ctxs[i]]
        x = states[ch]
        slot = x % m
        s = bisect_right(cdf, slot) - 1
        x = (cdf[s + 1] - cdf[s]) * (x // m) + slot - cdf[s]
        while x < L:
            pos -= 1
            if pos < 0:
                raise ValueError("corrupt packet: ran out of bytes")
            x = (x << 8) | data[pos]
        if s == esc_sym:
            pos -= esc_bytes
            if pos < 0:
                raise ValueError("corrupt packet: ran out of bytes")
            raws[i] = bytes(data[pos:pos + esc_bytes])
        states[ch] = x
        out[i] = s
    if pos != 0 or any(x != L for x in states):
        raise ValueError("corrupt packet: stream not fully consumed")
    return out, raws


# ---------------------------------------------------------------------------
# adaptive variant
# ---------------------------------------------------------------------------

def initial_cdf(cfg: TdmRansConfig) -> list[int]:
    return list(range(cfg.n_sym + 1))


def next_cdf(symbols: list[int], cfg: TdmRansConfig) -> list[int]:
    """Adaptive: table for the next packet from this packet's symbols."""
    counts = [0] * cfg.n_sym
    for s in symbols[: cfg.count_cap]:
        counts[s] += 1
    cdf = [0]
    for s in range(cfg.n_sym):
        cdf.append(cdf[-1] + counts[s] + 1)
    return cdf


class TdmRansEncoder:
    def __init__(self, cfg: TdmRansConfig = TdmRansConfig()) -> None:
        self.cfg = cfg
        self.cdf = initial_cdf(cfg)

    def encode_packet(self, chans: list[int], symbols: list[int]) -> bytes:
        out = encode_core(chans, symbols, [0] * len(symbols), [self.cdf], self.cfg)
        self.cdf = next_cdf(symbols, self.cfg)
        return out


class TdmRansDecoder:
    def __init__(self, cfg: TdmRansConfig = TdmRansConfig()) -> None:
        self.cfg = cfg
        self.cdf = initial_cdf(cfg)

    def decode_packet(self, data: bytes, chans: list[int]) -> list[int]:
        out = decode_core(data, chans, [0] * len(chans), [self.cdf], self.cfg)
        self.cdf = next_cdf(out, self.cfg)
        return out


# ---------------------------------------------------------------------------
# static variant
# ---------------------------------------------------------------------------

def uniform_static_cdf(cfg: TdmRansConfig) -> list[int]:
    """Reset contents of the RTL table memory: equal frequencies."""
    step = (1 << cfg.prob_bits) // cfg.n_sym
    return [s * step for s in range(cfg.n_sym)] + [1 << cfg.prob_bits]


def build_static_cdf(counts, cfg: TdmRansConfig) -> list[int]:
    """Offline training: histogram -> CDF summing to exactly 2^prob_bits, all f >= 1."""
    freqs = normalize_freqs(counts, 1 << cfg.prob_bits)
    cdf = [0]
    for f in freqs:
        cdf.append(cdf[-1] + f)
    return cdf


def check_static_cdf(cdf: list[int], cfg: TdmRansConfig) -> None:
    if len(cdf) != cfg.n_sym + 1 or cdf[0] != 0 or cdf[-1] != 1 << cfg.prob_bits:
        raise ValueError("table must have n_sym + 1 entries from 0 to 2^prob_bits")
    if any(b <= a for a, b in zip(cdf, cdf[1:])):
        raise ValueError("table must be strictly increasing (every f_s >= 1)")


class TdmRansStaticEncoder:
    def __init__(self, cfg: TdmRansConfig = TdmRansConfig(), cdfs=None) -> None:
        self.cfg = cfg
        self.cdfs = [uniform_static_cdf(cfg)] * cfg.n_ctx
        if cdfs is not None:
            self.load(cdfs)

    def load(self, cdfs: list[list[int]]) -> None:
        if len(cdfs) != self.cfg.n_ctx:
            raise ValueError(f"need {self.cfg.n_ctx} tables")
        for c in cdfs:
            check_static_cdf(c, self.cfg)
        self.cdfs = [list(c) for c in cdfs]

    def encode_packet(self, chans, symbols, ctxs) -> bytes:
        return encode_core(chans, symbols, ctxs, self.cdfs, self.cfg)


class TdmRansStaticDecoder(TdmRansStaticEncoder):
    def decode_packet(self, data, chans, ctxs) -> list[int]:
        return decode_core(data, chans, ctxs, self.cdfs, self.cfg)
