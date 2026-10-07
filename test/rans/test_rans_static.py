"""rans_tdm_static vs TdmRansStaticEncoder (model/nlc/rans_tdm.py), byte for byte."""

import cocotb
from cocotb.triggers import ClockCycles, FallingEdge

from rans_tb import (CFG, check_ii1, compare, drive, laplace, new_rng, reset,
                     round_robin, sink)
from nlc.rans_tdm import (TdmRansStaticDecoder, TdmRansStaticEncoder, build_static_cdf,
                          uniform_static_cdf)


def trained_tables(nrng, scales):
    out = []
    for sc in scales:
        sy = laplace(nrng, 20000, sc)
        out.append(build_static_cdf([sy.count(s) for s in range(64)], CFG))
    return out


async def load_tables(dut, cdfs):
    """Write every context's CDF through the load port (requires tbl_ready)."""
    await FallingEdge(dut.clk)
    assert int(dut.tbl_ready.value), "table load attempted while a packet is open"
    for k, cdf in enumerate(cdfs):
        for s in range(CFG.n_sym):
            dut.tbl_we.value = 1
            dut.tbl_addr.value = (k << 6) | s
            dut.tbl_data.value = cdf[s]
            await FallingEdge(dut.clk)
    dut.tbl_we.value = 0


async def wait_tbl_ready(dut):
    await FallingEdge(dut.clk)
    while not int(dut.tbl_ready.value):
        await FallingEdge(dut.clk)


def make_packets(nrng):
    pk = []
    for n_used, rounds in ((32, 6), (24, 10), (32, 25)):
        ch = round_robin(n_used, rounds)
        ctx = [int(k) for k in nrng.integers(0, CFG.n_ctx, len(ch))]
        pk.append((ch, laplace(nrng, len(ch)), ctx))
    return pk


async def run(dut, p_valid, p_ready, seed, table_sets):
    """table_sets[i]: tables to load before packet i (None = keep current)."""
    rng, nrng = new_rng(seed)
    packets = make_packets(nrng)
    enc = TdmRansStaticEncoder(CFG)
    expected = []
    for (ch, sy, ctx), tables in zip(packets, table_sets):
        if tables is not None:
            enc.load(tables)
        expected.append(enc.encode_packet(ch, sy, ctx))
    await reset(dut)

    async def between(i):
        if table_sets[i] is not None:
            await wait_tbl_ready(dut)
            await load_tables(dut, table_sets[i])

    accept_times = []
    cocotb.start_soon(drive(dut, packets, p_valid, rng, accept_times, between))
    got = await sink(dut, len(packets), p_ready, rng)
    compare(got, expected)
    dec = TdmRansStaticDecoder(CFG)
    for i, ((ch, sy, ctx), g, tables) in enumerate(zip(packets, got, table_sets)):
        if tables is not None:
            dec.load(tables)
        assert dec.decode_packet(g, ch, ctx) == sy, f"packet {i}: decode mismatch"
    return accept_times


@cocotb.test(timeout_time=5, timeout_unit="ms")
async def test_uniform_reset_table(dut):
    """No load at all: the reset contents must be the model's uniform table."""
    await run(dut, 0.8, 0.8, seed=3, table_sets=[None, None, None])


@cocotb.test(timeout_time=5, timeout_unit="ms")
async def test_loaded_tables_and_reload(dut):
    _, nrng = new_rng(4)
    a = trained_tables(nrng, (1.0, 2.0, 4.0, 8.0))
    b = trained_tables(nrng, (8.0, 4.0, 2.0, 1.0))
    await run(dut, 0.7, 0.6, seed=4, table_sets=[a, None, b])


@cocotb.test(timeout_time=5, timeout_unit="ms")
async def test_full_rate_ii1(dut):
    _, nrng = new_rng(5)
    a = trained_tables(nrng, (1.0, 2.0, 4.0, 8.0))
    check_ii1(await run(dut, 1.0, 1.0, seed=5, table_sets=[a, None, None]))


@cocotb.test(timeout_time=5, timeout_unit="ms")
async def test_writes_ignored_while_packet_open(dut):
    """A table write in the middle of a packet must not change the table."""
    rng, nrng = new_rng(6)
    ch = round_robin(32, 12)
    ctx = [0] * len(ch)
    sy = laplace(nrng, len(ch))
    expected = TdmRansStaticEncoder(CFG).encode_packet(ch, sy, ctx)
    await reset(dut)
    cocotb.start_soon(drive(dut, [(ch, sy, ctx)], 1.0, rng))

    async def rogue_write():
        await ClockCycles(dut.clk, 40)
        await FallingEdge(dut.clk)
        assert not int(dut.tbl_ready.value)
        dut.tbl_we.value = 1
        dut.tbl_addr.value = 33
        dut.tbl_data.value = 5            # would make table 0 invalid
        await FallingEdge(dut.clk)
        dut.tbl_we.value = 0

    cocotb.start_soon(rogue_write())
    got = await sink(dut, 1, 1.0, rng)
    compare(got, [expected])
    assert uniform_static_cdf(CFG)[33] != 5
