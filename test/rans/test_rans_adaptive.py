"""rans_tdm_adaptive vs TdmRansEncoder (model/nlc/rans_tdm.py), byte for byte."""

import cocotb

from rans_tb import (CFG, check_ii1, compare, drive, laplace, new_rng, reset,
                     round_robin, sink)
from nlc.rans_tdm import TdmRansDecoder, TdmRansEncoder


def make_packets(nrng):
    pk = []
    for n_used, rounds in ((32, 8), (24, 10), (32, 20)):
        ch = round_robin(n_used, rounds)
        pk.append((ch, laplace(nrng, len(ch)), None))
    ch = round_robin(32, 100)
    sy = [7] * len(ch)
    sy[5] = 63
    pk.append((ch, sy, None))                                    # near-degenerate table
    ch = round_robin(32, 130)
    pk.append((ch, [int(s) for s in nrng.integers(0, 64, len(ch))], None))   # count cap
    return pk


async def run(dut, p_valid, p_ready, seed):
    rng, nrng = new_rng(seed)
    packets = make_packets(nrng)
    enc = TdmRansEncoder(CFG)
    expected = [enc.encode_packet(ch, sy) for ch, sy, _ in packets]
    await reset(dut)
    accept_times = []
    cocotb.start_soon(drive(dut, packets, p_valid, rng, accept_times))
    got = await sink(dut, len(packets), p_ready, rng)
    compare(got, expected)
    dec = TdmRansDecoder(CFG)
    for i, ((ch, sy, _), g) in enumerate(zip(packets, got)):
        assert dec.decode_packet(g, ch) == sy, f"packet {i}: decode mismatch"
    return accept_times


@cocotb.test(timeout_time=5, timeout_unit="ms")
async def test_random_gaps_and_backpressure(dut):
    await run(dut, p_valid=0.7, p_ready=0.6, seed=1)


@cocotb.test(timeout_time=5, timeout_unit="ms")
async def test_full_rate_ii1(dut):
    check_ii1(await run(dut, p_valid=1.0, p_ready=1.0, seed=2))
