"""Register map. Mirror of src/nlc_regs.vh: keep in sync. Lossy mode only (D1)."""

from __future__ import annotations

CTRL = 0x00          # [7] enable, [1:0] mode (ignored by the hardware; write MODE_LOSSY)
N_SEL = 0x01
SEL_SLOT = 0x10

MODE_LOSSY = 0x01
CTRL_ENABLE = 0x80
N_SEL_MAX = 4


def config_writes(cfg: dict) -> list[tuple[int, int]]:
    """(addr, data) writes that configure the DUT for vectors/lossy/config.json."""
    if cfg["mode"] != MODE_LOSSY:
        raise ValueError(f"vectors for mode {cfg['mode']}: the hardware is lossy only (D1)")
    slots = cfg["slots"]
    if not 1 <= len(slots) <= N_SEL_MAX:
        raise ValueError(f"{len(slots)} channels, RTL supports 1..{N_SEL_MAX}")
    if any(b <= a for a, b in zip(slots, slots[1:])) or slots[-1] >= cfg["n_slots"]:
        raise ValueError(f"slots {slots} must be strictly ascending and < n_slots")
    w = [(CTRL, MODE_LOSSY), (N_SEL, len(slots))]
    w += [(SEL_SLOT + i, s) for i, s in enumerate(slots)]
    w.append((CTRL, MODE_LOSSY | CTRL_ENABLE))
    return w
