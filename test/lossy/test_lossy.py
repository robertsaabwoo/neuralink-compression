"""nlc_lossy vs LossyCodec (model/nlc/lossy.py), byte for byte, then decoded.

Also the power scenarios of docs/testing.md 4.4 (run at gate level by scripts/flow/flow.py
with a VCD; in RTL they are ordinary bit-exact tests):
  test_power_op      real data, n_sel 8, 2 packets   (C-PWR-1; VCD from frame 64)
  test_power_n4      real data, n_sel 4, 2 packets   (C-PWR-3)
  test_power_worst   generator LFSR, n_sel 8, 2 packets (C-PWR-5)
  test_power_floor   generator constant, n_sel 8, 1 packet (floor, info)
  idle (enable = 0) needs no simulation: no net toggles except the clock.
"""

import os
import random
import sys
from pathlib import Path

import cocotb
import numpy as np
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge, ReadOnly, RisingEdge

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "model"))
from nlc import stimgen  # noqa: E402
from nlc.data import load_challenge, synthetic  # noqa: E402
from nlc.lossy import LossyCodec, LossyConfig, default_tables  # noqa: E402
from nlc.packet import decode_stream, encode_stream  # noqa: E402

N_SEL = 8
CFG = LossyConfig(n_flush=N_SEL)     # the RTL flushes all N_SEL rANS states
FPP = CFG.frames_per_packet
LAG_FRAMES = 8          # a packet's last symbols leave 6 frames after its last frame
CLK_NS = float(os.environ.get("CLK_NS", "10"))   # gate level with unit delays: use 200 (5 MHz)
SPREAD = [3, 40, 41, 90, 128, 200, 254, 255]     # 8 of 256 slots, one slot per clock


def test_data(n_packets, n_sel=N_SEL, source="real", slots=SPREAD):
    n = n_packets * FPP + LAG_FRAMES
    if source in stimgen.PATTERNS:
        return stimgen.selected(source, slots[:n_sel], n)
    raw = ROOT / "data" / "raw"
    if raw.exists():
        _, x = load_challenge(raw, N_SEL, first_file=300)       # not in the ROM's training set
        return x[5000:5000 + n, :n_sel]
    return synthetic(n_sel, n, seed=11)


async def reset(dut, n_sel=N_SEL):
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    dut.rst_n.value = 0
    dut.enable.value = 0
    dut.n_sel.value = n_sel
    dut.smp_valid.value = 0
    dut.smp_data.value = 0
    dut.smp_ch.value = 0
    dut.smp_last.value = 0
    dut.m_ready.value = 0
    await ClockCycles(dut.clk, 3)
    dut.rst_n.value = 1
    dut.enable.value = 1
    await ClockCycles(dut.clk, 2)


async def drive(dut, x, slots, frame_cycles):
    """One frame every frame_cycles clocks; channel c in slot slots[c], one per clock."""
    await FallingEdge(dut.clk)
    for row in x:
        t = 0
        for c, s in enumerate(slots):
            if s > t:
                dut.smp_valid.value = 0
                await ClockCycles(dut.clk, s - t, rising=False)
                t = s
            dut.smp_valid.value = 1
            dut.smp_data.value = int(row[c])
            dut.smp_ch.value = c
            dut.smp_last.value = int(c == len(row) - 1)
            await FallingEdge(dut.clk)
            t += 1
        dut.smp_valid.value = 0
        await ClockCycles(dut.clk, frame_cycles - t, rising=False)


async def sink(dut, n_packets, p_ready, rng):
    pkts, cur = [], bytearray()
    while len(pkts) < n_packets:
        await FallingEdge(dut.clk)
        ready = rng.random() < p_ready
        dut.m_ready.value = int(ready)
        await ReadOnly()
        if ready and int(dut.m_valid.value):
            cur.append(int(dut.m_data.value))
            if int(dut.m_last.value):
                pkts.append(bytes(cur))
                cur = bytearray()
        await RisingEdge(dut.clk)
    return pkts


async def run(dut, n_packets, slots, frame_cycles, p_ready, seed, n_sel=N_SEL, source="real"):
    slots = slots[:n_sel]
    x = test_data(n_packets, n_sel, source, slots)
    codec = LossyCodec(CFG, default_tables())
    expected = encode_stream(x[:n_packets * FPP], codec)
    await reset(dut, n_sel)
    cocotb.start_soon(drive(dut, x, slots, frame_cycles))
    got = await sink(dut, n_packets, p_ready, random.Random(seed))
    for i, (g, e) in enumerate(zip(got, expected)):
        if g != e:
            k = next((k for k, (a, b) in enumerate(zip(g, e)) if a != b), min(len(g), len(e)))
            raise AssertionError(f"packet {i}: first difference at byte {k} "
                                 f"(got {len(g)} bytes, expected {len(e)})")
    assert not int(dut.overflow.value), "FIFO overflow"
    y = decode_stream(got, codec, n_sel)
    ref = x[:n_packets * FPP]
    err = (y - ref).astype(float)
    sig = (ref - ref.mean(axis=0)).astype(float)
    bits = 8 * sum(len(p) for p in got) / ref.size
    dut._log.info(f"{n_packets} packets: {bits:.3f} bits/sample, "
                  f"SNR {10 * np.log10((sig ** 2).sum() / (err ** 2).sum()):.1f} dB")


@cocotb.test(timeout_time=60, timeout_unit="sec")
async def test_adjacent_slots_short_frames(dut):
    """All channels back to back (one sample per clock), 64-slot frames, output stalls."""
    await run(dut, 2, list(range(N_SEL)), 64, 0.8, seed=1)


@cocotb.test(timeout_time=60, timeout_unit="sec")
async def test_spread_slots_256(dut):
    """256-slot mux at one slot per clock, channels spread over the frame."""
    await run(dut, 2, SPREAD, 256, 1.0, seed=2)


@cocotb.test(timeout_time=60, timeout_unit="sec")
async def test_power_window(dut):
    """The operating point used for power: 8 of 256 slots, one slot per clock, one packet."""
    await run(dut, 1, SPREAD, 256, 1.0, seed=3)


@cocotb.test(timeout_time=120, timeout_unit="sec")
async def test_power_op(dut):
    """Power scenario op: real data, 8 channels, 2 packets."""
    await run(dut, 2, SPREAD, 256, 1.0, seed=4)


@cocotb.test(timeout_time=120, timeout_unit="sec")
async def test_power_n4(dut):
    """Power scenario op4: real data, 4 channels, 2 packets."""
    await run(dut, 2, SPREAD, 256, 1.0, seed=5, n_sel=4)


@cocotb.test(timeout_time=120, timeout_unit="sec")
async def test_power_worst(dut):
    """Power scenario worst: generator LFSR on the selected slots, 8 channels, 2 packets."""
    await run(dut, 2, SPREAD, 256, 1.0, seed=6, source="lfsr")


@cocotb.test(timeout_time=120, timeout_unit="sec")
async def test_power_floor(dut):
    """Power scenario floor: generator constant, 8 channels, 1 packet."""
    await run(dut, 1, SPREAD, 256, 1.0, seed=7, source="constant")
