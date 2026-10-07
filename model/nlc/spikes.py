"""Modes 2 and 3: spike front end, binned spike counts, spike-band power.

Front end (per channel, runs continuously, never reset per packet):
  lp  = lp_acc >> hp_shift
  hp  = x - lp                                  first-order EMA high-pass
  lp_acc += x - lp                              fc ~ fs / (2*pi*2^hp_shift)
  spike when hp <= -thr[c] and refractory counter is 0, then counter = refractory

Mode 2 payload (one packet per `bin_len` samples), channels in groups of `group`:
  all counts zero -> '0'
  otherwise       -> '1' + truncated_unary(count, c_max) for each channel in group

Mode 3 payload (one packet per `sbp_len` samples), per channel:
  v = min(sum(metric(hp)) >> sbp_shift, 2^sbp_bits - 1); v < sbp_floor -> 0
  v == 0 -> '0', else '1' + v on sbp_bits
  metric 'abs' (|hp|, no multiplier) or 'square' (hp^2, true power).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .bitstream import BitReader, BitWriter


@dataclass(frozen=True)
class FrontEndConfig:
    adc_bits: int = 10
    hp_shift: int = 3        # ~780 Hz corner at 19.5 kHz
    refractory: int = 20     # samples, ~1 ms


@dataclass(frozen=True)
class BinnedConfig:
    bin_len: int = 400       # samples, ~20 ms
    group: int = 8
    c_max: int = 7           # 3-bit saturating counters


@dataclass(frozen=True)
class SbpConfig:
    sbp_len: int = 400
    metric: str = "abs"
    sbp_shift: int = 6
    sbp_bits: int = 8
    sbp_floor: int = 0


class SpikeFrontEnd:
    def __init__(self, cfg: FrontEndConfig, thresholds) -> None:
        self.cfg = cfg
        self.thr = [int(t) for t in thresholds]
        self.reset()

    def reset(self) -> None:
        n = len(self.thr)
        self.lp_acc = [(1 << (self.cfg.adc_bits - 1)) << self.cfg.hp_shift] * n
        self.ref = [0] * n

    def step(self, frame) -> tuple[list[int], list[bool]]:
        """Process one frame (one sample per channel); return (hp, spike)."""
        s = self.cfg.hp_shift
        hps, spikes = [], []
        for c, x in enumerate(frame):
            x = int(x)
            lp = self.lp_acc[c] >> s
            hp = x - lp
            self.lp_acc[c] += hp
            spike = False
            if self.ref[c]:
                self.ref[c] -= 1
            elif hp <= -self.thr[c]:
                spike = True
                self.ref[c] = self.cfg.refractory
            hps.append(hp)
            spikes.append(spike)
        return hps, spikes


def highpass(x: np.ndarray, cfg: FrontEndConfig) -> np.ndarray:
    """Vectorised-over-channels version of the front-end filter (calibration only)."""
    s = cfg.hp_shift
    lp_acc = np.full(x.shape[1], (1 << (cfg.adc_bits - 1)) << s, dtype=np.int64)
    out = np.empty_like(x, dtype=np.int64)
    for t in range(x.shape[0]):
        hp = x[t].astype(np.int64) - (lp_acc >> s)
        lp_acc += hp
        out[t] = hp
    return out


def calibrate_thresholds(x: np.ndarray, cfg: FrontEndConfig, k: float = 4.5) -> np.ndarray:
    """Software calibration (done off-chip, loaded into threshold registers):
    thr = k * sigma, sigma estimated as median(|hp|) / 0.6745 (Quiroga 2004)."""
    hp = highpass(x, cfg)
    sigma = np.median(np.abs(hp), axis=0) / 0.6745
    return np.maximum(1, np.round(k * sigma)).astype(np.int64)


class BinnedSpikeCodec:
    mode = 2

    def __init__(self, fe_cfg: FrontEndConfig, cfg: BinnedConfig, thresholds) -> None:
        self.cfg = cfg
        self.frames_per_packet = cfg.bin_len
        self.fe = SpikeFrontEnd(fe_cfg, thresholds)

    def reset(self) -> None:
        self.fe.reset()

    def counts(self, block: np.ndarray) -> list[int]:
        cnt = [0] * block.shape[1]
        for frame in block:
            _, spikes = self.fe.step(frame)
            for c, s in enumerate(spikes):
                if s and cnt[c] < self.cfg.c_max:
                    cnt[c] += 1
        return cnt

    def encode_payload(self, bw: BitWriter, block: np.ndarray) -> None:
        cnt = self.counts(block)
        g = self.cfg.group
        for base in range(0, len(cnt), g):
            grp = cnt[base:base + g]
            if not any(grp):
                bw.write(0, 1)
                continue
            bw.write(1, 1)
            for v in grp:
                bw.write_truncated_unary(v, self.cfg.c_max)

    def decode_payload(self, br: BitReader, n_frames: int, n_ch: int) -> np.ndarray:
        g = self.cfg.group
        out = []
        for base in range(0, n_ch, g):
            width = min(g, n_ch - base)
            if not br.read(1):
                out += [0] * width
            else:
                out += [br.read_unary(limit=self.cfg.c_max) for _ in range(width)]
        return np.array(out, dtype=np.int64)[None, :]


class SpikeBandPowerCodec:
    mode = 3

    def __init__(self, fe_cfg: FrontEndConfig, cfg: SbpConfig, thresholds=None) -> None:
        if cfg.metric not in ("abs", "square"):
            raise ValueError(cfg.metric)
        self.cfg = cfg
        self.frames_per_packet = cfg.sbp_len
        self.fe_cfg = fe_cfg
        self._thr = thresholds
        self.fe: SpikeFrontEnd | None = None

    def reset(self) -> None:
        self.fe = None

    def values(self, block: np.ndarray) -> list[int]:
        cfg = self.cfg
        if self.fe is None:
            thr = self._thr if self._thr is not None else [1 << 30] * block.shape[1]
            self.fe = SpikeFrontEnd(self.fe_cfg, thr)
        acc = [0] * block.shape[1]
        for frame in block:
            hps, _ = self.fe.step(frame)
            for c, h in enumerate(hps):
                acc[c] += abs(h) if cfg.metric == "abs" else h * h
        vmax = (1 << cfg.sbp_bits) - 1
        vals = [min(a >> cfg.sbp_shift, vmax) for a in acc]
        return [0 if v < cfg.sbp_floor else v for v in vals]

    def encode_payload(self, bw: BitWriter, block: np.ndarray) -> None:
        for v in self.values(block):
            if v:
                bw.write(1, 1)
                bw.write(v, self.cfg.sbp_bits)
            else:
                bw.write(0, 1)

    def decode_payload(self, br: BitReader, n_frames: int, n_ch: int) -> np.ndarray:
        vals = [br.read(self.cfg.sbp_bits) if br.read(1) else 0 for _ in range(n_ch)]
        return np.array(vals, dtype=np.int64)[None, :]
