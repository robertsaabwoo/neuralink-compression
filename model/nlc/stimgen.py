"""256-slot stimulus generator: model of the on-chip test peripheral (src/nlc_stimgen.v).

The generator plays the ADC mux on chip (docs/testing.md 4.2): one slot per clock, slots
0..255, slot 0 flagged as the frame start, so the compressor sees the real interface at full
rate without the pins. Deterministic from reset, so this model gives the exact stream.

Patterns (value of slot `s` in frame `f`, 10 bits):
  RAMP      (f + 4 s) mod 1024                       trivially checkable, per-channel slope 1
  LFSR      bits [9:0] of a 16-bit Galois LFSR (taps 0xB400, seed 0xACE1), stepped once per
            slot, value taken before the step               incompressible: worst case
  CONSTANT  512                                      minimum activity (power floor)
"""

from __future__ import annotations

import numpy as np

N_SLOTS = 256
RAMP, LFSR, CONSTANT = 0, 1, 2
PATTERNS = {"ramp": RAMP, "lfsr": LFSR, "constant": CONSTANT}
LFSR_SEED = 0xACE1
LFSR_TAPS = 0xB400


def lfsr_seq(n: int, seed: int = LFSR_SEED) -> np.ndarray:
    out = np.empty(n, dtype=np.int64)
    x = seed
    for i in range(n):
        out[i] = x
        x = (x >> 1) ^ (LFSR_TAPS if x & 1 else 0)
    return out


def stream(pattern: int | str, n_frames: int) -> np.ndarray:
    """(n_frames, 256) slot values, frame 0 = the first frame after reset."""
    p = PATTERNS.get(pattern, pattern) if isinstance(pattern, str) else pattern
    f = np.arange(n_frames, dtype=np.int64)[:, None]
    s = np.arange(N_SLOTS, dtype=np.int64)[None, :]
    if p == RAMP:
        return (f + 4 * s) & 0x3FF
    if p == LFSR:
        return (lfsr_seq(n_frames * N_SLOTS) & 0x3FF).reshape(n_frames, N_SLOTS)
    if p == CONSTANT:
        return np.full((n_frames, N_SLOTS), 512, dtype=np.int64)
    raise ValueError(f"unknown pattern {pattern}")


def selected(pattern: int | str, slots: list[int], n_frames: int) -> np.ndarray:
    """(n_frames, len(slots)): what the compressor sees for the selected slots."""
    return stream(pattern, n_frames)[:, slots]
