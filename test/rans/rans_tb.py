"""Shared cocotb driver/sink for the rans_tdm_* encoders."""

import random
import sys
from pathlib import Path

import cocotb
import numpy as np
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge, ReadOnly, RisingEdge
from cocotb.utils import get_sim_time

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "model"))
from nlc.rans_tdm import TdmRansConfig  # noqa: E402

CFG = TdmRansConfig()            # must match the RTL parameter defaults
PIPE_DEPTH = CFG.lsh + 8 + 3
CLK_NS = 10


def round_robin(n_used, rounds):
    return [c for _ in range(rounds) for c in range(n_used)]


def laplace(rng, n, scale=3.0):
    v = rng.laplace(0, scale, n).round().astype(int) + 32
    return [int(s) for s in v.clip(0, 63)]


async def reset(dut):
    assert CFG.n_ch >= PIPE_DEPTH
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    for name in ("s_tvalid", "s_tdata", "s_tchan", "s_tlast", "m_tready",
                 "s_tctx", "tbl_we", "tbl_addr", "tbl_data"):
        if hasattr(dut, name):
            getattr(dut, name).value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 5)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 2)


async def drive(dut, packets, p_valid, rng, accept_times=None, between=None):
    """packets: list of (chans, symbols, ctxs or None). `between(i)` is awaited
    before packet i (e.g. to load tables)."""
    for p, (chans, syms, ctxs) in enumerate(packets):
        if between is not None:
            await between(p)
        times = []
        for i, (c, s) in enumerate(zip(chans, syms)):
            await FallingEdge(dut.clk)
            while rng.random() > p_valid:
                dut.s_tvalid.value = 0
                await FallingEdge(dut.clk)
            dut.s_tvalid.value = 1
            dut.s_tdata.value = s
            dut.s_tchan.value = c
            if ctxs is not None:
                dut.s_tctx.value = ctxs[i]
            dut.s_tlast.value = int(i == len(chans) - 1)
            while True:
                await ReadOnly()
                ok = int(dut.s_tready.value)
                await RisingEdge(dut.clk)
                if ok:
                    times.append(get_sim_time(unit="ns"))
                    break
                await FallingEdge(dut.clk)
        await FallingEdge(dut.clk)
        dut.s_tvalid.value = 0
        if accept_times is not None:
            accept_times.append(times)


async def sink(dut, n_packets, p_ready, rng):
    pkts, cur = [], bytearray()
    while len(pkts) < n_packets:
        await FallingEdge(dut.clk)
        ready = rng.random() < p_ready
        dut.m_tready.value = int(ready)
        await ReadOnly()
        if ready and int(dut.m_tvalid.value):
            keep, d = int(dut.m_tkeep.value), int(dut.m_tdata.value)
            assert keep in (1, 3), f"bad m_tkeep {keep:02b}"
            cur.append(d & 0xFF)
            if keep == 3:
                cur.append(d >> 8)
            if int(dut.m_tlast.value):
                pkts.append(bytes(cur))
                cur = bytearray()
        await RisingEdge(dut.clk)
    return pkts


def compare(got, expected):
    for i, (g, e) in enumerate(zip(got, expected)):
        if g != e:
            n = next((k for k, (a, b) in enumerate(zip(g, e)) if a != b), min(len(g), len(e)))
            raise AssertionError(f"packet {i}: first difference at byte {n} "
                                 f"(got {len(g)} bytes, expected {len(e)})")


def check_ii1(accept_times):
    for i, times in enumerate(accept_times):
        gaps = {round(b - a) for a, b in zip(times, times[1:])}
        assert gaps == {CLK_NS}, f"packet {i}: accept spacing {sorted(gaps)} ns, expected {CLK_NS}"


def new_rng(seed):
    return random.Random(seed), np.random.default_rng(seed)
