"""Input data: Neuralink compression-challenge WAVs and a synthetic generator.

Challenge data: one 16-bit mono WAV per electrode (~5 s, ~19.5 kHz). The samples
look like 10-bit ADC codes rescaled to 16 bits with a non-integer step
(~65535/1023 = 64.06). `wav_to_adc` infers that step, maps back to 10-bit
codes, and *verifies* the mapping is a bijection, so lossless results on codes
are lossless on the WAV. Treat the inferred step as a hypothesis until it passes
on the full dataset.

Files are separate electrodes; stacking N of them as channels is a stand-in for
a real simultaneous N-channel frame stream, not a true multichannel recording.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np


def read_wav(path: str | Path) -> tuple[int, np.ndarray]:
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1:
            raise ValueError(f"{path}: expected 16-bit mono")
        fs = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    return fs, x.astype(np.int64)


def wav_to_adc(x16: np.ndarray, adc_bits: int = 10) -> tuple[np.ndarray, dict[int, int]]:
    """Return (codes, lut) with lut[code] == original 16-bit value."""
    uniq = np.unique(x16)
    if len(uniq) == 1:
        return np.full_like(x16, 1 << (adc_bits - 1)), {1 << (adc_bits - 1): int(uniq[0])}
    span = uniq[-1] - uniq[0]
    gaps = np.diff(uniq)
    adjacent = gaps[gaps < 1.5 * np.median(gaps)]          # consecutive codes only
    step = span / max(1, round(span / adjacent.mean()))
    rel = np.round((x16 - uniq[0]) / step).astype(np.int64)
    n_levels = int(rel.max()) + 1
    if n_levels > 1 << adc_bits:
        raise ValueError(f"{n_levels} levels do not fit in {adc_bits} bits (step={step:.3f})")
    codes = rel + ((1 << adc_bits) - n_levels) // 2
    lut: dict[int, int] = {}
    for c, v in zip(codes.tolist(), x16.tolist()):
        if lut.setdefault(c, v) != v:
            raise ValueError(f"step {step:.3f} maps two WAV values to code {c}")
    return codes, lut


def load_challenge(data_dir: str | Path, n_channels: int, first_file: int = 0,
                   adc_bits: int = 10) -> tuple[int, np.ndarray]:
    """Stack n_channels WAV files as channels -> (fs, codes[T, C])."""
    files = sorted(Path(data_dir).glob("*.wav"))[first_file:first_file + n_channels]
    if len(files) < n_channels:
        raise FileNotFoundError(f"need {n_channels} WAVs in {data_dir}, found {len(files)}")
    rates, chans = set(), []
    for f in files:
        fs, x16 = read_wav(f)
        rates.add(fs)
        chans.append(wav_to_adc(x16, adc_bits)[0])
    if len(rates) != 1:
        raise ValueError(f"mixed sample rates {rates}")
    t = min(len(c) for c in chans)
    return rates.pop(), np.stack([c[:t] for c in chans], axis=1)


def synthetic(n_channels: int, n_samples: int, fs: int = 19531, seed: int = 0,
              adc_bits: int = 10, noise_lsb: float = 6.0, lfp_lsb: float = 40.0,
              spike_lsb: float = 60.0, rate_hz: float = 20.0) -> np.ndarray:
    """Cheap stand-in for CI: random-walk LFP + white noise + biphasic spikes."""
    rng = np.random.default_rng(seed)
    lfp = np.cumsum(rng.normal(0, 1, (n_samples, n_channels)), axis=0)
    lfp = lfp - lfp.mean(axis=0)
    lfp *= lfp_lsb / (lfp.std(axis=0) + 1e-9)
    x = lfp + rng.normal(0, noise_lsb, (n_samples, n_channels))
    t = np.arange(-10, 20)
    template = -np.exp(-((t / 3.0) ** 2)) + 0.4 * np.exp(-(((t - 8) / 5.0) ** 2))
    n_spk = rng.poisson(rate_hz * n_samples / fs, n_channels)
    for c in range(n_channels):
        for t0 in rng.integers(10, n_samples - 20, n_spk[c]):
            x[t0 - 10:t0 + 20, c] += spike_lsb * template
    x += 1 << (adc_bits - 1)
    return np.clip(np.round(x), 0, (1 << adc_bits) - 1).astype(np.int64)
