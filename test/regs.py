"""Register map and fixed hardware constants. Mirror of src/nlc_regs.vh and the
localparams in src/nlc_encoder.v: keep in sync."""

from __future__ import annotations

CTRL = 0x00
N_SEL = 0x01
FPP = 0x02
WIN_LO = 0x03
WIN_HI = 0x04
SBP_SHIFT = 0x05
SEL_SLOT = 0x10
THR = 0x20

CTRL_ENABLE = 0x80
N_SEL_MAX = 8

# Fixed in RTL; vectors must be generated with exactly these values.
HW = {
    "rice": dict(adc_bits=10, k_max=9, q_limit=16, n_reset=64, a_init=16),
    "frontend": dict(adc_bits=10, hp_shift=3, refractory=20),
    "binned": dict(group=4, c_max=7),
    "sbp": dict(metric="abs", sbp_bits=8, sbp_floor=0),
}


def check_hw_constants(cfg: dict) -> None:
    for block, fixed in HW.items():
        for key, value in fixed.items():
            if block in cfg and cfg[block][key] != value:
                raise ValueError(f"vectors use {block}.{key}={cfg[block][key]}, RTL has {value}")


def config_writes(cfg: dict) -> list[tuple[int, int]]:
    """(addr, data) writes that configure the DUT for a vectors/<mode>/config.json."""
    check_hw_constants(cfg)
    slots = cfg["slots"]
    if not 1 <= len(slots) <= N_SEL_MAX:
        raise ValueError(f"{len(slots)} channels, RTL supports 1..{N_SEL_MAX}")
    if any(b <= a for a, b in zip(slots, slots[1:])) or slots[-1] >= cfg["n_slots"]:
        raise ValueError(f"slots {slots} must be strictly ascending and < n_slots")
    mode = cfg["mode"]
    w = [(CTRL, mode), (N_SEL, len(slots))]
    w += [(SEL_SLOT + i, s) for i, s in enumerate(slots)]
    if "rice" in cfg:
        w.append((FPP, cfg["rice"]["frames_per_packet"]))
    for key in ("binned", "sbp"):
        if key in cfg:
            win = cfg[key]["bin_len" if key == "binned" else "sbp_len"]
            w += [(WIN_LO, win & 0xFF), (WIN_HI, win >> 8)]
    if "sbp" in cfg:
        w.append((SBP_SHIFT, cfg["sbp"]["sbp_shift"]))
    for i, t in enumerate(cfg.get("thresholds", [])):
        if not 0 <= t <= 255:
            raise ValueError(f"threshold {t} does not fit the 8-bit register")
        w.append((THR + i, t))
    w.append((CTRL, mode | CTRL_ENABLE))
    return w
