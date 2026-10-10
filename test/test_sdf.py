"""T-GL-3 self-check: the timing checks of the SDF gate-level run are live (GATES=sdf, CVC).

A real host changes the pins away from the clock edge (harness.py: falling edge), so the main
run should see no setup/hold violation. To show that a quiet log means "checked" and not
"not checking", this test moves cfg_en (uio[5], into a 2-flop synchroniser) a little later
after each rising edge, 0 to 4 ns in 20 ps steps: one of the steps lands inside the first
synchroniser flop's setup/hold window (clock-tree latency + wire delay), which the simulator
must report. Run on its own (the X it causes is the point), not in the default modules.
"""

import cocotb
from cocotb.triggers import RisingEdge, Timer

from harness import CFG_EN, reset


@cocotb.test()
async def timing_checks_live(dut):
    pins = await reset(dut)
    level = 0
    for step in range(200):
        await RisingEdge(dut.clk)
        await Timer(20 * step + 1, unit="ps")
        level ^= 1
        pins.set_uio(1 << CFG_EN, level << CFG_EN)
