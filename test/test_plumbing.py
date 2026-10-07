"""Tests of the provided I/O plumbing (pins, config, slot selection).

They probe internal signals, so they are RTL-only (skipped for GATES=yes),
and they pass with any encoder, including the empty skeleton.
"""

import cocotb
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

import regs
from harness import GATES, SLOT_CYCLES, drive_adc, reset, write_reg


@cocotb.test(skip=GATES)
async def test_config_registers(dut):
    pins = await reset(dut)
    for addr, data in [(regs.N_SEL, 3), (regs.SEL_SLOT + 2, 0xA5), (regs.FPP, 32),
                       (regs.WIN_LO, 0x34), (regs.WIN_HI, 0x12), (regs.THR + 7, 0x5C),
                       (regs.CTRL, 0x02)]:
        await write_reg(pins, addr, data)
    cfg = dut.user_project.core.u_cfg
    assert int(cfg.n_sel.value) == 3
    assert (int(cfg.sel_slots.value) >> 16) & 0xFF == 0xA5
    assert int(cfg.fpp.value) == 32
    assert int(cfg.win_len.value) == 0x1234
    assert int(cfg.thr.value) >> 56 == 0x5C
    assert int(cfg.mode.value) == 2 and int(cfg.enable.value) == 0


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
