"""Tests of the provided I/O plumbing (pins, config, slot selection).

Most probe internal signals, so they are RTL-only (skipped for GATES=yes), and pass with
any encoder, including the empty skeleton. The DFT tests (readback, overflow pin) use the
pins only; the overflow test needs the real encoder.
"""

import os
import random

import cocotb
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

import regs
from harness import GATES, SLOT_CYCLES, drive_adc, read_reg, reset, write_reg


@cocotb.test(skip=GATES)
async def test_config_registers(dut):
    pins = await reset(dut)
    cfg = dut.user_project.core.u_cfg
    # slot registers have no reset (written before enable): give them known values first
    for i in range(regs.N_SEL_MAX):
        await write_reg(pins, regs.SEL_SLOT + i, 0x11 * (i + 1))
    reset_slots = int(cfg.sel_slots.value)
    # retired addresses of the model-only modes (0x02-0x05 fpp/window/sbp, 0x20+ thresholds)
    # must not touch anything; CTRL's mode bits are ignored (lossy only, D1)
    for addr, data in [(regs.N_SEL, 3), (regs.SEL_SLOT + 2, 0xA5), (0x02, 0x20), (0x03, 0x34),
                       (0x05, 0x1F), (0x27, 0x5C), (regs.CTRL, 0x02)]:
        await write_reg(pins, addr, data)
    assert int(cfg.n_sel.value) == 3
    slots = int(cfg.sel_slots.value)
    assert (slots >> 16) & 0xFF == 0xA5
    assert slots & ~(0xFF << 16) == reset_slots & ~(0xFF << 16)
    assert int(cfg.enable.value) == 0
    await write_reg(pins, regs.CTRL, regs.CTRL_ENABLE | 0x03)
    assert int(cfg.enable.value) == 1


@cocotb.test(skip=GATES)
async def test_slot_selector(dut):
    """Selected slots come out as channels 0..n-1, in order, starting at a frame."""
    slots, n_slots = [2, 5, 9], 12
    pins = await reset(dut)
    await write_reg(pins, regs.N_SEL, len(slots))
    for i, s in enumerate(slots):
        await write_reg(pins, regs.SEL_SLOT + i, s)
    await write_reg(pins, regs.CTRL, regs.CTRL_ENABLE)

    sel = dut.user_project.core.u_sel
    seen = []

    async def monitor():
        while True:
            await RisingEdge(dut.clk)
            await ReadOnly()
            if int(sel.smp_valid.value):
                seen.append((int(sel.smp_ch.value), int(sel.smp_data.value),
                             int(sel.smp_first.value), int(sel.smp_last.value)))

    cocotb.start_soon(monitor())
    frames = [[100 * f + s for s in slots] for f in range(5)]
    # start mid-frame: the partial frame before the first s_frame must be ignored
    await drive_adc(pins, [[999, 999, 999]] + frames, slots, n_slots, start_slot=7)
    await ClockCycles(dut.clk, 2 * SLOT_CYCLES)

    expected = [(i, v, int(i == 0), int(i == len(slots) - 1))
                for fr in frames for i, v in enumerate(fr)]
    assert seen == expected, f"\n got {seen}\n exp {expected}"


@cocotb.test()
async def test_config_readback(dut):
    """DFT: config registers read back on uo_out (cfg_en = 1, address strobe, enable = 0);
    a readback writes nothing. Pins only, so it also runs at gate level."""
    pins = await reset(dut)
    slots = [3, 9, 17, 40, 77, 100, 126, 127]
    await write_reg(pins, regs.N_SEL, 5)
    for i, s in enumerate(slots):
        await write_reg(pins, regs.SEL_SLOT + i, s)
    assert await read_reg(pins, regs.N_SEL) == 5
    for i, s in enumerate(slots):
        assert await read_reg(pins, regs.SEL_SLOT + i) == s, f"slot register {i}"
    assert await read_reg(pins, regs.CTRL) == 0
    assert await read_reg(pins, 0x02) == 0                       # unused address
    assert await read_reg(pins, regs.N_SEL) == 5                 # readbacks wrote nothing
    assert not int(dut.m_valid.value)


@cocotb.test(skip=os.environ.get("ENCODER") == "replay")
async def test_overflow_pin(dut):
    """DFT: the sticky overflow flag reaches uio[4]. 8 channels in 8-slot frames (16 clocks,
    far below C-IF-9's 64) make the coder fall a frame behind; enable = 0 clears it."""
    pins = await reset(dut)
    n = regs.N_SEL_MAX
    await write_reg(pins, regs.N_SEL, n)
    for i in range(n):
        await write_reg(pins, regs.SEL_SLOT + i, i)
    await write_reg(pins, regs.CTRL, regs.MODE_LOSSY | regs.CTRL_ENABLE)
    assert not int(dut.overflow.value)
    rng = random.Random(1)
    frames = [[rng.randrange(1024) for _ in range(n)] for _ in range(40)]
    await drive_adc(pins, frames, list(range(n)), n)
    await ClockCycles(dut.clk, 4)
    assert int(dut.overflow.value), "coder a frame behind for 40 frames, overflow pin low"
    await write_reg(pins, regs.CTRL, regs.MODE_LOSSY)
    await ClockCycles(dut.clk, 4)
    assert not int(dut.overflow.value), "overflow not cleared by enable = 0"
