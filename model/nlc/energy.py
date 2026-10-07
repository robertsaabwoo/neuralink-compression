"""Break-even model: does a mode's compute energy pay for the radio bits it saves?

Per raw sample:
  saved  = (raw_bits - compressed_bits) * e_radio_bit
  net    = saved - e_compress_sample
Mode pays for itself when net > 0. e_compress_sample comes from post-synthesis
power analysis (activity-annotated) divided by sample throughput.
"""

from __future__ import annotations

from dataclasses import dataclass

E_RADIO_BIT = 10e-9  # J/bit, BLE figure used for N1-class budgets


@dataclass(frozen=True)
class BreakEven:
    bits_per_sample: float
    saved_j_per_sample: float
    compress_j_per_sample: float

    @property
    def net_j_per_sample(self) -> float:
        return self.saved_j_per_sample - self.compress_j_per_sample

    @property
    def pays_off(self) -> bool:
        return self.net_j_per_sample > 0


def break_even(raw_bits_per_sample: float, compressed_bits_per_sample: float,
               compress_j_per_sample: float, e_radio_bit: float = E_RADIO_BIT) -> BreakEven:
    saved = (raw_bits_per_sample - compressed_bits_per_sample) * e_radio_bit
    return BreakEven(compressed_bits_per_sample, saved, compress_j_per_sample)


def max_compute_budget(raw_bits_per_sample: float, compressed_bits_per_sample: float,
                       e_radio_bit: float = E_RADIO_BIT) -> float:
    """Largest compute energy per sample (J) at which the mode still breaks even."""
    return (raw_bits_per_sample - compressed_bits_per_sample) * e_radio_bit
