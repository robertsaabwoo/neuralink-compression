"""Mode 0: lossless delta prediction + adaptive-k Rice coding.

Per channel, per packet: the first sample is sent raw on B bits and does not
touch the adaptation state (a midscale prediction would inject one huge residual
into A and inflate k for ~100+ samples). Every later sample (integer, shifts only,
so it maps 1:1 onto RTL):

  e  = wrap(x[n] - x[n-1])            modular residual in [-2^(B-1), 2^(B-1))
  m  = zigzag(e)                      0,-1,1,-2,2 ... -> 0,1,2,3,4 ...   (B bits)
  k  = min{k : N << k >= A}, <= k_max JPEG-LS style mean tracker
  q  = m >> k
  if q < q_limit:  unary(q) + (m & (2^k-1)) on k bits
  else:            q_limit ones + m on B bits       (escape; bounds codeword length)
  A += |e|; N += 1; if N == n_reset: A >>= 1, N >>= 1

With M = 2^k the patent's truncated-binary remainder degenerates to plain k-bit
binary, which is why there is no divider. Max codeword = q_limit + B bits.

Per-channel state (x_prev, A, N) resets at every packet so packets decode
independently if the radio drops one.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .bitstream import BitReader, BitWriter


@dataclass(frozen=True)
class RiceConfig:
    adc_bits: int = 10
    k_max: int = 9
    q_limit: int = 16
    n_reset: int = 64
    a_init: int = 16        # JPEG-LS: max(2, (RANGE + 32) >> 6) = 16 for 10 bits
    frames_per_packet: int = 128

    @property
    def max_codeword_bits(self) -> int:
        return self.q_limit + self.adc_bits


def wrap(e: int, bits: int) -> int:
    half = 1 << (bits - 1)
    return ((e + half) & ((1 << bits) - 1)) - half


def zigzag(e: int) -> int:
    return 2 * e if e >= 0 else -2 * e - 1


def unzigzag(m: int) -> int:
    return m >> 1 if not m & 1 else -((m + 1) >> 1)


def select_k(a: int, n: int, k_max: int) -> int:
    k = 0
    while (n << k) < a and k < k_max:
        k += 1
    return k


class _ChannelState:
    __slots__ = ("x_prev", "a", "n")

    def __init__(self, cfg: RiceConfig) -> None:
        self.x_prev = None
        self.a = cfg.a_init
        self.n = 1

    def update(self, e: int, cfg: RiceConfig) -> None:
        self.a += abs(e)
        self.n += 1
        if self.n == cfg.n_reset:
            self.a >>= 1
            self.n >>= 1


class LosslessCodec:
    mode = 0

    def __init__(self, cfg: RiceConfig = RiceConfig()) -> None:
        self.cfg = cfg
        self.frames_per_packet = cfg.frames_per_packet

    def reset(self) -> None:
        pass  # all state is per packet

    def encode_payload(self, bw: BitWriter, block: np.ndarray) -> None:
        cfg = self.cfg
        n_ch = block.shape[1]
        states = [_ChannelState(cfg) for _ in range(n_ch)]
        for frame in block:
            for c in range(n_ch):
                st = states[c]
                x = int(frame[c])
                if st.x_prev is None:
                    bw.write(x, cfg.adc_bits)
                    st.x_prev = x
                    continue
                e = wrap(x - st.x_prev, cfg.adc_bits)
                m = zigzag(e)
                k = select_k(st.a, st.n, cfg.k_max)
                q = m >> k
                if q < cfg.q_limit:
                    bw.write_unary(q)
                    bw.write(m & ((1 << k) - 1), k)
                else:
                    bw.write((1 << cfg.q_limit) - 1, cfg.q_limit)
                    bw.write(m, cfg.adc_bits)
                st.update(e, cfg)
                st.x_prev = x

    def decode_payload(self, br: BitReader, n_frames: int, n_ch: int) -> np.ndarray:
        cfg = self.cfg
        mask = (1 << cfg.adc_bits) - 1
        states = [_ChannelState(cfg) for _ in range(n_ch)]
        out = np.empty((n_frames, n_ch), dtype=np.int64)
        for t in range(n_frames):
            for c in range(n_ch):
                st = states[c]
                if st.x_prev is None:
                    st.x_prev = out[t, c] = br.read(cfg.adc_bits)
                    continue
                k = select_k(st.a, st.n, cfg.k_max)
                q = br.read_unary(limit=cfg.q_limit)
                if q < cfg.q_limit:
                    m = (q << k) | br.read(k)
                else:
                    m = br.read(cfg.adc_bits)
                e = unzigzag(m)
                x = (st.x_prev + e) & mask
                st.update(e, cfg)
                st.x_prev = x
                out[t, c] = x
        return out
