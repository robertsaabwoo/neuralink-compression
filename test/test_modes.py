"""Bit-exact DUT vs golden model through the TT pins.

Silicon is lossy mode only (decision D1, docs/constraints.md); modes 0/2/3 live in the
Python model only. Under `make ENCODER=replay` the same tests replay the golden bytes
through the real pins to show the harness itself passes.
Lossy through the pins at 5 MHz, 1 slot / 2 clocks, 64-clock frames = T-IF-4.
"""

import cocotb

from harness import run_vector_test

TIMEOUT = dict(timeout_time=100, timeout_unit="ms")  # backstop; tests fail fast on their own


@cocotb.test(**TIMEOUT)
async def test_lossy(dut):
    """T-IF-4: lossy through the TT pins; the host captures every valid clock (D8: no ack, the
    former test_lossy_host_never_waits is this test now)."""
    await run_vector_test(dut, "lossy")

