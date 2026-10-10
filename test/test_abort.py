"""Abort token and clean resume through the TT pins (D5/D7, T-ROB-2/T-ROB-4 at the top level).

The token is uio[7] m_last = 1 with uio[6] m_valid = 0 for one clock (src/project.v). These
tests run at RTL and at gate level (`make GATES=yes` / `GATES=local`): pins only, no probes.
  test_abort_disable      enable = 0 while packet 0 is partly out: one token, nothing more
                          until enable; then the vector run from the start is bit-exact
                          (T-ROB-2: the next run == a run from reset).
  test_abort_short_frame  frame 120 of packet 0 is short (s_frame before the last selected
                          slots): one token after packet 0's partial bytes, packet 0 dropped,
                          packet 1 bit-exact against the vectors (blocks and coder states
                          restart at every packet, D5; the short frame counts as one frame).
"""

import os

import cocotb
from cocotb.triggers import ClockCycles, FallingEdge

import regs
from harness import (ABORT, FLUSH_CLOCKS, SLOT_CYCLES, configure, drive_adc, load_vectors,
                     read_events, reset, write_reg)

# the replay mock (make ENCODER=replay) replays the golden bytes: it has no abort path
TIMEOUT = dict(timeout_time=100, timeout_unit="ms", skip=os.environ.get("ENCODER") == "replay")


def packets(stream: list) -> list[list[tuple[int, int]]]:
    """Split (byte, last) pairs into packets (a trailing partial one is dropped)."""
    out, cur = [], []
    for b in stream:
        cur.append(b)
        if b[1]:
            out.append(cur)
            cur = []
    return out


async def wait_bytes(dut, events: list, n: int, max_clocks: int) -> None:
    for _ in range(max_clocks):
        if sum(e != ABORT for e in events) >= n:
            return
        await FallingEdge(dut.clk)


@cocotb.test(**TIMEOUT)
async def test_abort_disable(dut):
    """D7 at the pins: disable mid-packet -> abort token; re-enable -> run from reset."""
    frames, expected, cfg = load_vectors("lossy")
    pins = await reset(dut)
    await configure(pins, cfg)
    ev: list = []
    reader = cocotb.start_soon(read_events(pins, ev))
    await drive_adc(pins, frames[:150], cfg["slots"], cfg["n_slots"])
    partial = [e for e in ev if e != ABORT]
    assert ev and ABORT not in ev, f"before disable: {len(partial)} bytes, events {ev[-3:]}"
    assert not any(last for _, last in partial), "packet 0 complete before frame 150"
    await write_reg(pins, regs.CTRL, regs.MODE_LOSSY)                   # enable = 0
    await ClockCycles(dut.clk, 50)
    assert ev.count(ABORT) == 1, f"{ev.count(ABORT)} abort tokens after disable mid-packet"
    assert ev[-1] == ABORT and ev[:-1] == partial, "bytes after the abort token while disabled"
    assert not int(dut.overflow.value), "overflow pin set by a disable (only a lagging coder)"

    ev.clear()
    await write_reg(pins, regs.CTRL, regs.MODE_LOSSY | regs.CTRL_ENABLE)
    await drive_adc(pins, frames, cfg["slots"], cfg["n_slots"])
    await wait_bytes(dut, ev, len(expected), max(FLUSH_CLOCKS, 200 * SLOT_CYCLES))
    await ClockCycles(dut.clk, 20 * SLOT_CYCLES)
    reader.cancel()
    assert ABORT not in ev, "abort token in the run after re-enable"
    assert ev == expected, (
        f"after re-enable: {len(ev)}/{len(expected)} bytes, first difference at byte "
        f"{next((i for i, (g, e) in enumerate(zip(ev, expected)) if g != e), min(len(ev), len(expected)))}")
    assert not int(dut.overflow.value)


@cocotb.test(**TIMEOUT)
async def test_abort_short_frame(dut):
    """D5 at the pins: a short frame in packet 0 -> abort token, packet 1 bit-exact."""
    frames, expected, cfg = load_vectors("lossy")
    slots, n_slots = cfg["slots"], cfg["n_slots"]
    short, cut = 120, slots[-2]          # frame 120 ends before its last 2 selected slots
    exp_pk = packets(expected)
    assert len(exp_pk) >= 2, "vectors hold fewer than 2 packets"
    pins = await reset(dut)
    await configure(pins, cfg)
    ev: list = []
    reader = cocotb.start_soon(read_events(pins, ev))
    await drive_adc(pins, frames[:short], slots, n_slots)
    n_before = len(ev)
    assert n_before and ABORT not in ev, f"no byte of packet 0 out before frame {short}"
    where = {s: i for i, s in enumerate(slots)}
    for s in range(cut):                 # the short frame: slots 0 .. cut-1 only
        v = frames[short][where[s]] if s in where else 0
        await pins.strobe(v, frame=int(s == 0))
        await ClockCycles(dut.clk, SLOT_CYCLES - 2)
    await drive_adc(pins, frames[short + 1:], slots, n_slots)
    await wait_bytes(dut, ev, n_before + len(exp_pk[1]), max(FLUSH_CLOCKS, 200 * SLOT_CYCLES))
    await ClockCycles(dut.clk, 20 * SLOT_CYCLES)
    reader.cancel()

    assert ev.count(ABORT) == 1, f"{ev.count(ABORT)} abort tokens for one short frame"
    k = ev.index(ABORT)
    assert k >= n_before and not any(e[1] for e in ev[:k]), "packet 0 completed: not aborted"
    got = packets(ev[k + 1:])
    assert got, "no packet after the abort token"
    assert got[0][0][0] & 0x3F == 1, f"resumed packet seq {got[0][0][0] & 0x3F}, expected 1"
    assert got[0] == exp_pk[1], (
        f"packet 1 after the abort: {len(got[0])} bytes, expected {len(exp_pk[1])}, first "
        f"difference at byte {next((i for i, (g, e) in enumerate(zip(got[0], exp_pk[1])) if g != e), None)}")
    assert len(got) == 1, f"{len(got)} packets after the abort, expected 1"
    assert not int(dut.overflow.value), "overflow pin set by a short frame (only a lagging coder)"
