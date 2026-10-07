"""Mode 1: bior2.2 DWT + deadzone quantisation + static multi-context rANS,
in the order the streaming hardware (src/nlc_lossy.sv) produces it.

Transform
---------
bior2.2 analysis filters are the CDF/LeGall 5/3 pair, so the transform is the
JPEG 2000 reversible integer lifting scheme (shifts and adds only):

  d[i] = s[2i+1] - ((s[2i] + s[2i+2]) >> 1)
  a[i] = s[2i]   + ((d[i-1] + d[i] + 2) >> 2)        symmetric extension

A block is `block_len` frames; a packet is `blocks_per_packet` blocks. Per
channel each block is centred
(x - 2^(B-1)) and transformed over `levels`; each subband is quantised with a
power-of-two step, q = sign(c) * (|c| >> shift); the approximation band is
delta-coded (the first one in a block against 0).

Streaming order (what the hardware does, no block buffer)
---------------------------------------------------------
Every level is a 3-register lifting stage (last even, pending odd, last d).
A pair (a[i], d[i]) is complete when s[2i+2] arrives, or at the last (odd)
input of the block, which mirrors s[n] -> s[n-2]. Level l+1 takes level l's
a outputs as its input. On each input sample the channel pushes, in this
order, whichever of these became ready:

  d1, d2, ..., dL, delta(aL)

so a channel's coefficients form a FIFO in "production order". Production is
bursty (0 to levels+1 per sample) but averages exactly one per sample, so the
hardware pops exactly one per channel per sample (a small FIFO absorbs the
bursts) and the rANS coder sees one symbol per input sample, round-robin over
channels, the same as the input. With a 64-frame block, 3 levels, the FIFO
peaks at 7 entries and a block's last symbols leave 6 frames into the next.

Symbols
-------
Context 0 = approximation deltas, context l = detail level l (1 = finest).
A value v with |v| <= s_max is symbol v + s_max; anything else is ESC
(= 2 s_max + 1) carrying zigzag(v) as `esc_bytes` raw little-endian bytes
(nlc.rans_tdm: raw bytes go before the symbol's renorm bytes).

Payload
-------
The packet's blocks, each channel's symbols concatenated block after block
(its FIFO order), sent j-major then channel (symbol j of channel 0, symbol j
of channel 1, ...), coded by the TDM static rANS of nlc.rans_tdm (one state per
channel, one table per context): renorm/raw bytes, then the final state of
channels 0..n_flush-1, ceil((prob_bits + lsh + 8) / 8) bytes each. No length
field: the packet ends at the byte stream's end-of-packet flag.

The state flush is the main overhead (8 ch x 3 bytes per packet), hence
several blocks per packet and a small state: lsh = 2 gives a 22-bit state
and a 10-stage divider at no measurable cost in ratio (lsh = 11: 31 bits).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .bitstream import BitReader, BitWriter
from .lossless import unzigzag, zigzag
from .rans_tdm import (TdmRansConfig, build_static_cdf, check_static_cdf, decode_core_raw,
                       encode_core)

# ---------------------------------------------------------------------------
# block transform (reference)
# ---------------------------------------------------------------------------


def lift53_fwd(s: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    s = np.asarray(s, dtype=np.int64)
    even, odd = s[0::2], s[1::2]
    even_next = np.append(even[1:], even[-1])            # s[n] mirrors to s[n-2]
    d = odd - ((even + even_next) >> 1)
    d_prev = np.insert(d[:-1], 0, d[0])                  # d[-1] mirrors to d[0]
    a = even + ((d_prev + d + 2) >> 2)
    return a, d


def lift53_inv(a: np.ndarray, d: np.ndarray) -> np.ndarray:
    d_prev = np.insert(d[:-1], 0, d[0])
    even = a - ((d_prev + d + 2) >> 2)
    even_next = np.append(even[1:], even[-1])
    odd = d + ((even + even_next) >> 1)
    s = np.empty(2 * len(a), dtype=np.int64)
    s[0::2], s[1::2] = even, odd
    return s


def dwt_fwd(s: np.ndarray, levels: int) -> list[np.ndarray]:
    """Returns [a_L, d_L, ..., d_1]."""
    details = []
    a = np.asarray(s, dtype=np.int64)
    for _ in range(levels):
        a, d = lift53_fwd(a)
        details.append(d)
    return [a] + details[::-1]


def dwt_inv(bands: list[np.ndarray]) -> np.ndarray:
    a = bands[0]
    for d in bands[1:]:
        a = lift53_inv(a, d)
    return a


def quantize(c, shift: int):
    return np.sign(c) * (np.abs(c) >> shift)


def dequantize(q: np.ndarray, shift: int) -> np.ndarray:
    if shift == 0:
        return q.astype(np.int64)
    mag = (np.abs(q) << shift) + (1 << (shift - 1))      # mid-point reconstruction
    return np.where(q == 0, 0, np.sign(q) * mag)


# ---------------------------------------------------------------------------
# streaming transform (what the RTL computes)
# ---------------------------------------------------------------------------


class Lift53Stage:
    """One lifting level over a block of n inputs: 3 registers (e, o, dp)."""

    def __init__(self, n: int) -> None:
        self.n = n
        self.i = 0
        self.e = self.o = self.dp = 0

    def push(self, v: int) -> tuple[int, int, int] | None:
        """Returns (pair index, a, d) when a pair completes."""
        i, self.i = self.i, self.i + 1
        if i & 1:
            self.o = v
            return self._pair(i >> 1, self.e) if i == self.n - 1 else None
        if i == 0:
            self.e = v
            return None
        out = self._pair((i >> 1) - 1, v)
        self.e = v
        return out

    def _pair(self, p: int, e_next: int) -> tuple[int, int, int]:
        d = self.o - ((self.e + e_next) >> 1)
        dp = d if p == 0 else self.dp
        a = self.e + ((dp + d + 2) >> 2)
        self.dp = d
        return p, a, d


def stream_coeffs(s, levels: int) -> list[list[tuple[int, int, int]]]:
    """Per input sample, the (ctx, index, coefficient) pushed, in push order."""
    stages = [Lift53Stage(len(s) >> l) for l in range(levels)]
    out = []
    for v in s:
        pushed, x = [], int(v)
        for l, st in enumerate(stages):
            r = st.push(x)
            if r is None:
                break
            p, x, d = r
            pushed.append((l + 1, p, d))
            if l == levels - 1:
                pushed.append((0, p, x))
        out.append(pushed)
    return out


# ---------------------------------------------------------------------------
# configuration, schedule
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LossyConfig:
    adc_bits: int = 10
    block_len: int = 64
    levels: int = 3
    approx_shift: int = 1
    detail_shifts: tuple[int, ...] = (3, 2, 2)   # level 1 (finest) .. level L
    s_max: int = 31
    esc_bytes: int = 2
    prob_bits: int = 12
    lsh: int = 2
    blocks_per_packet: int = 4
    n_flush: int | None = None                   # final states per packet (None: n_ch)

    @property
    def n_sym(self) -> int:
        return 2 * self.s_max + 2               # values -s_max..s_max, then ESC

    @property
    def esc(self) -> int:
        return 2 * self.s_max + 1

    @property
    def n_ctx(self) -> int:
        return self.levels + 1

    def shift_for_ctx(self, ctx: int) -> int:
        return self.approx_shift if ctx == 0 else self.detail_shifts[ctx - 1]

    @property
    def frames_per_packet(self) -> int:
        return self.block_len * self.blocks_per_packet

    @property
    def state_bytes(self) -> int:
        return -(-(self.prob_bits + self.lsh + 8) // 8)

    def rans_cfg(self, n_ch: int) -> TdmRansConfig:
        return TdmRansConfig(n_sym=self.n_sym, n_ch=self.n_flush or n_ch, lsh=self.lsh,
                             prob_bits=self.prob_bits, n_ctx=self.n_ctx,
                             state_bytes=self.state_bytes)


def layout(cfg: LossyConfig) -> list[tuple[int, int]]:
    """(ctx, band index) of a channel's symbols in production (= FIFO) order."""
    return [(k, p) for pushed in stream_coeffs(np.zeros(cfg.block_len, dtype=np.int64),
                                               cfg.levels) for k, p, _ in pushed]


def push_schedule(cfg: LossyConfig) -> list[int]:
    """Symbols pushed per channel at each frame of the block."""
    return [len(p) for p in stream_coeffs(np.zeros(cfg.block_len, dtype=np.int64), cfg.levels)]


def fifo_profile(cfg: LossyConfig, n_blocks: int = 4) -> tuple[int, int]:
    """(peak FIFO occupancy after a push, frames a block's last symbol lags its end)
    with one pop per channel per frame. Same for every channel."""
    pushes = push_schedule(cfg)
    occ = peak = 0
    for _ in range(n_blocks):
        for n in pushes:
            occ += n
            peak = max(peak, occ)
            occ -= occ > 0
    return peak, occ


def _check_cfg(cfg: LossyConfig) -> None:
    if cfg.block_len % (1 << cfg.levels):
        raise ValueError("block_len must be a multiple of 2^levels")
    if len(cfg.detail_shifts) != cfg.levels:
        raise ValueError("need one detail shift per level")
    if cfg.esc_bytes * 8 < cfg.adc_bits + cfg.levels + 2:
        raise ValueError("esc_bytes too small for the widest delta")


# ---------------------------------------------------------------------------
# codec
# ---------------------------------------------------------------------------


def channel_symbols(x: np.ndarray, cfg: LossyConfig) -> list[tuple[int, int]]:
    """(context, value) pairs for one channel's block, in production order."""
    s = x.astype(np.int64) - (1 << (cfg.adc_bits - 1))
    out, prev = [], 0
    for pushed in stream_coeffs(s, cfg.levels):
        for k, p, c in pushed:
            q = int(quantize(c, cfg.shift_for_ctx(k)))
            if k == 0:
                q, prev = q - (prev if p else 0), q
            out.append((k, q))
    return out


_channel_symbols = channel_symbols       # scripts/compare_rans_tables.py


def symbol_of(v: int, cfg: LossyConfig) -> tuple[int, bytes | None]:
    if abs(v) <= cfg.s_max:
        return v + cfg.s_max, None
    return cfg.esc, zigzag(v).to_bytes(cfg.esc_bytes, "little")


def train_tables(x: np.ndarray, cfg: LossyConfig) -> list[list[int]]:
    """Static CDFs (one per context) from calibration data x (T, C). In hardware: the table RAM."""
    hist = np.zeros((cfg.n_ctx, cfg.n_sym), dtype=np.int64)
    for t0 in range(0, x.shape[0] - cfg.block_len + 1, cfg.block_len):
        for c in range(x.shape[1]):
            for k, v in channel_symbols(x[t0:t0 + cfg.block_len, c], cfg):
                hist[k, symbol_of(v, cfg)[0]] += 1
    rcfg = cfg.rans_cfg(1)
    return [build_static_cdf(h, rcfg) for h in hist]


TABLES_PATH = Path(__file__).with_name("lossy_tables.json")


def default_tables() -> list[list[int]]:
    """The tables in the hardware ROM (scripts/gen_lossy_rom.py)."""
    return json.loads(TABLES_PATH.read_text())["cdfs"]


class LossyCodec:
    mode = 1

    def __init__(self, cfg: LossyConfig, tables: list[list[int]]) -> None:
        _check_cfg(cfg)
        if len(tables) != cfg.n_ctx:
            raise ValueError(f"need {cfg.n_ctx} tables")
        for t in tables:
            check_static_cdf(t, cfg.rans_cfg(1))
        self.cfg = cfg
        self.tables = [list(t) for t in tables]
        self.frames_per_packet = cfg.frames_per_packet
        self._layout = [(b, k, p) for b in range(cfg.blocks_per_packet) for k, p in layout(cfg)]

    def reset(self) -> None:
        pass

    def encode_payload(self, bw: BitWriter, block: np.ndarray) -> None:
        cfg, n_ch = self.cfg, block.shape[1]
        B = cfg.block_len
        per_ch = [[kv for b in range(cfg.blocks_per_packet)
                   for kv in channel_symbols(block[b * B:(b + 1) * B, c], cfg)]
                  for c in range(n_ch)]
        chans, ctxs, syms, raws = [], [], [], []
        for j in range(len(self._layout)):
            for c in range(n_ch):
                k, v = per_ch[c][j]
                s, raw = symbol_of(v, cfg)
                chans.append(c)
                ctxs.append(k)
                syms.append(s)
                raws.append(raw)
        bw.write_bytes(encode_core(chans, syms, ctxs, self.tables, cfg.rans_cfg(n_ch), raws))

    def decode_payload(self, br: BitReader, n_frames: int, n_ch: int) -> np.ndarray:
        cfg = self.cfg
        data = br.read_bytes(br.bits_left // 8)
        chans = [c for _ in self._layout for c in range(n_ch)]
        ctxs = [k for _, k, _ in self._layout for _ in range(n_ch)]
        syms, raws = decode_core_raw(data, chans, ctxs, self.tables, cfg.rans_cfg(n_ch),
                                     cfg.esc, cfg.esc_bytes)
        out = np.empty((n_frames, n_ch), dtype=np.int64)
        B = cfg.block_len
        for c in range(n_ch):
            bands = [[np.zeros(B >> cfg.levels, dtype=np.int64)]
                     + [np.zeros(B >> l, dtype=np.int64) for l in range(cfg.levels, 0, -1)]
                     for _ in range(cfg.blocks_per_packet)]
            prev = 0
            for j, (b, k, p) in enumerate(self._layout):
                s, raw = syms[j * n_ch + c], raws[j * n_ch + c]
                v = unzigzag(int.from_bytes(raw, "little")) if s == cfg.esc else s - cfg.s_max
                if k == 0:
                    v = prev = v + (prev if p else 0)
                bands[b][0 if k == 0 else cfg.levels + 1 - k][p] = v
            for b, bb in enumerate(bands):
                bb = [dequantize(q, cfg.shift_for_ctx(0 if i == 0 else cfg.levels + 1 - i))
                      for i, q in enumerate(bb)]
                y = dwt_inv(bb) + (1 << (cfg.adc_bits - 1))
                out[b * B:(b + 1) * B, c] = np.clip(y, 0, (1 << cfg.adc_bits) - 1)
        return out
