"""T-PWR-3: power scenarios through the TT pins, for the routed netlist (flow.py step layout).

Not in the default COCOTB_TEST_MODULES (TT's `test` and `gl_test` actions stay short); run with
  make COCOTB_TEST_MODULES=test_power [GATES=local NETLIST=<routed nl.v> FST= PLUSARGS=+vcd=..]

Operating point through the pins: 8 channels at 128 slots x 2 clocks = 256 clocks per frame,
i.e. the real frame rate (19.5 kHz) at 5 MHz, real data (test/vectors/power, made by
scripts/gen_vectors.py --name power --source real ...). The bytes are checked against the model,
so the routed netlist is also verified functionally on real data.
"""

import cocotb
from cocotb.triggers import ClockCycles, ReadOnly

from harness import drive_adc, load_vectors, reset, run_vector_test, write_reg
import regs

TIMEOUT = dict(timeout_time=2000, timeout_unit="ms")


@cocotb.test(**TIMEOUT)
async def t_pwr3_op(dut):
    """Real data, 2 packets, the host captures every valid clock (VCD from frame 64)."""
    await run_vector_test(dut, "power")


@cocotb.test(**TIMEOUT)
async def t_pwr3_op_quiet(dut):
    """As t_pwr3_op, unselected slots carry 0 (a host that only moves the data pins for
    the selected channels): separates the chip from the cost of emulating the ADC mux."""
    await run_vector_test(dut, "power", filler="zero")


@cocotb.test(**TIMEOUT)
async def t_pwr3_idle(dut):
    """Configured, CTRL enable = 0, ADC strobes keep coming: no output (C-PWR-2)."""
    await _idle(dut, "random")


@cocotb.test(**TIMEOUT)
async def t_pwr3_idle_quiet(dut):
    """As t_pwr3_idle, unselected slots carry 0."""
    await _idle(dut, "zero")


async def _idle(dut, filler: str) -> None:
    frames, _, cfg = load_vectors("power")
    pins = await reset(dut)
    for addr, data in regs.config_writes(cfg)[:-1]:            # all but the enable write
        await write_reg(pins, addr, data)
    await drive_adc(pins, frames[:64], cfg["slots"], cfg["n_slots"], filler=filler)
    await ReadOnly()
    assert not int(dut.m_valid.value), "output while disabled"
    await ClockCycles(dut.clk, 2)
