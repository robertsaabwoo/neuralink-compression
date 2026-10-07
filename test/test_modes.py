"""Bit-exact DUT vs golden model, one test per hardware mode.

Silicon is lossy mode only (decision D1, docs/constraints.md): modes 0/2/3 are skipped
with that reason, except under `make ENCODER=replay`, which replays the golden bytes through
the real pins/FIFO to show the harness itself passes for every mode.
Lossy (mode 1) through the pins at 5 MHz, 1 slot / 2 clocks, 64-clock frames = T-IF-4.
"""

import os

import cocotb

from harness import run_vector_test

TIMEOUT = dict(timeout_time=100, timeout_unit="ms")  # backstop; tests fail fast on their own
NOT_IN_SILICON = dict(skip=os.environ.get("ENCODER", "user") != "replay")  # D1


@cocotb.test(**TIMEOUT, **NOT_IN_SILICON)
async def test_lossless(dut):
    await run_vector_test(dut, "lossless")


@cocotb.test(**TIMEOUT, **NOT_IN_SILICON)
async def test_binned(dut):
    await run_vector_test(dut, "binned")


@cocotb.test(**TIMEOUT)
async def test_lossy(dut):
    """T-IF-4: lossy through the TT pins."""
    await run_vector_test(dut, "lossy")


@cocotb.test(**TIMEOUT, **NOT_IN_SILICON)
async def test_sbp(dut):
    await run_vector_test(dut, "sbp")


@cocotb.test(**TIMEOUT, **NOT_IN_SILICON)
async def test_lossless_host_never_waits(dut):
    """Host acks immediately: checks the FIFO path at full read rate."""
    await run_vector_test(dut, "lossless", max_ack_delay=0)
