"""DFT debug modes (decision D11, register DBG): T-IF-8 raw bypass, T-IF-9 clock-gate
enable observation. Through the TT pins. T-IF-8 derives its expected bytes from the driven
samples only; T-IF-9's per-gate check reads the gate enables in the RTL hierarchy (skipped
at gate level, where its pin-level part runs).
"""

import os
import random
import sys
from pathlib import Path

import cocotb
from cocotb.triggers import ClockCycles, FallingEdge

import regs
from harness import (GATES, configure, drive_adc, load_vectors, read_reg, reset,
                     run_vectors_on, write_reg)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "dft"))
import icg_map  # noqa: E402


def raw_expected(frames: list[list[int]]) -> list[tuple[int, int]]:
    """Mode 1 format, from the driven samples: per frame, per selected channel in slot order,
    {6'b0, s[9:8]} then s[7:0]; m_last on the frame's last byte."""
    out = []
    for fr in frames:
        for i, v in enumerate(fr):
            out += [(v >> 8, 0), (v & 0xFF, int(i == len(fr) - 1))]
    return out


async def capture(dut, got: list, stop: list) -> None:
    """Host: (byte, last) at every falling edge with m_valid; abort tokens as (None, 1)."""
    while not stop:
        await FallingEdge(dut.clk)
        if int(dut.m_valid.value):
            got.append((int(dut.m_data.value), int(dut.m_last.value)))
        elif int(dut.m_last.value):
            got.append((None, 1))


async def setup(pins, slots: list[int], dbg: int) -> None:
    """Configure n_sel + slots, DBG, then enable."""
    await write_reg(pins, regs.N_SEL, len(slots))
    for i, s in enumerate(slots):
        await write_reg(pins, regs.SEL_SLOT + i, s)
    await write_reg(pins, regs.DBG, dbg)
    await write_reg(pins, regs.CTRL, regs.MODE_LOSSY | regs.CTRL_ENABLE)


def walk(n_frames: int, k: int, seed: int) -> list[list[int]]:
    """Random-walk channels (smooth, like neural data: no aborts at the pin rate)."""
    rng = random.Random(seed)
    x = [rng.randrange(256, 768) for _ in range(k)]
    out = []
    for _ in range(n_frames):
        x = [min(1023, max(0, v + rng.randrange(-12, 13))) for v in x]
        out.append(list(x))
    return out


@cocotb.test(timeout_time=400, timeout_unit="ms")
async def t_if_8_raw_bypass(dut):
    """T-IF-8: DBG mode 1 sends the selected channels' raw samples: 8 adjacent slots, full
    10-bit range; expected bytes from the driven samples (not from the RTL). A DBG write
    while enabled is ignored; DBG reads back; then normal mode is bit-exact again."""
    pins = await reset(dut)
    slots, n_slots = list(range(8, 16)), 32           # 8 adjacent slots, 64-clock frames
    await setup(pins, slots, regs.DBG_RAW)
    await write_reg(pins, regs.DBG, regs.DBG_NORMAL)   # enabled: ignored (stays raw)
    rng = random.Random(8)
    frames = [[rng.randrange(1024) for _ in slots] for _ in range(30)]
    frames += [[0x3FF] * 8, [0] * 8, [0x155, 0x2AA, 0x100, 0x0FF, 0x200, 0x1FF, 0x300, 0x0FF]]
    got, stop = [], []
    cap = cocotb.start_soon(capture(dut, got, stop))
    await drive_adc(pins, frames, slots, n_slots)
    await ClockCycles(dut.clk, 8)
    stop.append(1)
    await cap.join()
    exp = raw_expected(frames)
    bad = next((i for i, (g, e) in enumerate(zip(got, exp)) if g != e), None)
    assert bad is None, f"raw byte {bad}: got {got[bad]}, expected {exp[bad]}"
    assert len(got) == len(exp), f"{len(got)} raw bytes, expected {len(exp)}"
    assert not int(dut.overflow.value), "overflow in raw mode on the pin path"

    # disable; DBG reads back as written; normal mode: the compressed stream is bit-exact
    await write_reg(pins, regs.CTRL, regs.MODE_LOSSY)
    assert await read_reg(pins, regs.DBG) == regs.DBG_RAW
    await write_reg(pins, regs.DBG, 0xA0 | regs.DBG_NORMAL)
    assert await read_reg(pins, regs.DBG) == 0xA0
    await configure(pins, load_vectors("lossy")[2])
    await run_vectors_on(pins, "lossy")


@cocotb.test(timeout_time=2000, timeout_unit="ms",
             skip=os.environ.get("ENCODER") == "replay")   # the replay mock has no gates
async def t_if_9_icg_observe(dut):
    """T-IF-9: DBG mode 2 shows the enables of gate group DBG[7:4] on uo_out one clock later,
    m_valid every clock. Idle (enable = 0): only the always-on gates are open (pins, also at
    gate level). RTL: every bit of all 16 groups against the gate's `en` in the hierarchy
    (scripts/dft/icg_map.py) while compressing 8 channels, and every gate seen open."""
    pins = await reset(dut)
    slots, n_slots = [1, 4, 6, 11, 20, 33, 47, 60], 64
    await write_reg(pins, regs.N_SEL, len(slots))
    for i, s in enumerate(slots):
        await write_reg(pins, regs.SEL_SLOT + i, s)

    # idle, group 15: the always-on pin gate (bit 1) and the output register's gate (bit 6,
    # open every clock in mode 2); nothing else
    await write_reg(pins, regs.DBG, 0xF0 | regs.DBG_ICG)
    for _ in range(20):
        await FallingEdge(dut.clk)
        assert int(dut.m_valid.value) and not int(dut.m_last.value), "m_valid in mode 2"
        assert int(dut.m_data.value) == 0x42, f"idle group 15 = {int(dut.m_data.value):02x}"
    for g in range(15):                               # idle: every core gate closed
        await write_reg(pins, regs.DBG, g << 4 | regs.DBG_ICG)
        await FallingEdge(dut.clk)
        assert int(dut.m_data.value) == 0, f"idle group {g} = {int(dut.m_data.value):02x}"
    if GATES:                                         # no hierarchy in a netlist
        return

    top = dut.user_project
    ens = {}
    for e in icg_map.table():
        h = top
        for part in e["rtl"].split("."):
            name, _, idx = part.partition("[")
            h = getattr(h, name)
            if idx:
                h = h[int(idx[:-1])]
        ens[(e["group"], e["bit"])] = h.en

    seen: dict[tuple[int, int], set[int]] = {}
    for g in range(16):
        await write_reg(pins, regs.DBG, g << 4 | regs.DBG_ICG)
        await write_reg(pins, regs.CTRL, regs.MODE_LOSSY | regs.CTRL_ENABLE)
        adc = cocotb.start_soon(drive_adc(pins, walk(24, len(slots), g), slots, n_slots))
        prev = None
        while not adc.done():
            await FallingEdge(dut.clk)
            assert int(dut.m_valid.value) and not int(dut.m_last.value), "m_valid in mode 2"
            got = int(dut.m_data.value)
            assert prev is None or got == prev, (
                f"group {g}: uo_out {got:08b}, gate enables {prev:08b}")
            prev = 0
            for b in range(8):
                if (g, b) in ens:
                    v = int(ens[(g, b)].value)
                    prev |= v << b
                    seen.setdefault((g, b), set()).add(v)
        await write_reg(pins, regs.CTRL, regs.MODE_LOSSY)   # disable before the next DBG
        assert not int(dut.overflow.value)
    never = {k for k, v in seen.items() if 1 not in v}
    # never open while running: config write gates (no writes then) and the raw-bypass gate
    assert never == {(13, 7), (15, 0), (15, 4), (15, 5)} | {(14, b) for b in range(8)}, (
        f"gates never seen open: {sorted(never)}")
    assert len(seen) == len(icg_map.table())
