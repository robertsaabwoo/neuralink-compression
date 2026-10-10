"""cocotb harness: plays the ADC mux and the host, checks bytes against the golden model.

The pin protocol is defined in src/project.v. Summary:
  ui_in = sample[7:0] / config byte; uio[1:0] = sample[9:8]
  uio[2] s_strobe, uio[3] s_frame, uio[5] cfg_en  (inputs)
  uio[4] overflow, uio[6] m_valid, uio[7] m_last, uo_out = m_data  (outputs, registered)

The ADC side never waits: one strobe every SLOT_CYCLES clocks, n_slots slots
per frame, selected channels at their slots and random filler elsewhere.
"""

from __future__ import annotations

import json
import os
import random
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge, First, ReadOnly, RisingEdge

import regs

VECTORS = Path(__file__).parent / "vectors"
CLK_NS = float(os.environ.get("CLK_NS", 200))          # D2: 5 MHz, the ADC slot rate
# Pin path (C-IF-9): at most one slot every 2 clocks, frames >= 64 clocks (vectors: 32 slots)
SLOT_CYCLES = int(os.environ.get("SLOT_CYCLES", 2))
FLUSH_CLOCKS = 2000                                    # lossy: lag frames + packet flush
GATES = os.environ.get("GATES") in ("yes", "local")   # gate level: no internal probes

S_STROBE, S_FRAME, CFG_EN = 2, 3, 5        # uio[4]: overflow output (was m_ack; D8: no ack)


class Pins:
    """Single owner of uio_in so the ADC driver and the host reader can't
    clobber each other's bits. All inputs change on the falling edge."""

    def __init__(self, dut) -> None:
        self.dut = dut
        self.uio = 0

    def set_uio(self, mask: int, value: int) -> None:
        self.uio = (self.uio & ~mask) | (value & mask)
        self.dut.uio_in.value = self.uio

    async def strobe(self, data: int, frame: int = 0) -> None:
        """One strobe: high for one clock, then low. Caller adds the gap."""
        await FallingEdge(self.dut.clk)
        self.dut.ui_in.value = data & 0xFF
        self.set_uio(0b11 | 1 << S_FRAME | 1 << S_STROBE,
                     (data >> 8) & 0b11 | frame << S_FRAME | 1 << S_STROBE)
        await FallingEdge(self.dut.clk)
        self.set_uio(1 << S_STROBE | 1 << S_FRAME, 0)


async def reset(dut) -> Pins:
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    pins = Pins(dut)
    dut.ena.value = 1
    dut.ui_in.value = 0
    pins.set_uio(0xFF, 0)
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 10)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 2)
    return pins


async def write_reg(pins: Pins, addr: int, data: int) -> None:
    pins.set_uio(1 << CFG_EN, 1 << CFG_EN)
    await pins.strobe(addr)
    await pins.strobe(data)
    await FallingEdge(pins.dut.clk)
    pins.set_uio(1 << CFG_EN, 0)
    # the register is written 3 clocks after the data strobe's edge (2-flop synchroniser,
    # edge detect, cfg_we): settle before the caller reads it back
    await ClockCycles(pins.dut.clk, 3)


async def read_reg(pins: Pins, addr: int) -> int:
    """Config readback (CTRL.enable = 0): cfg_en = 1, strobe the address, read uo_out, drop
    cfg_en without a data strobe (nothing is written)."""
    pins.set_uio(1 << CFG_EN, 1 << CFG_EN)
    await pins.strobe(addr)
    await ClockCycles(pins.dut.clk, 4)
    await FallingEdge(pins.dut.clk)
    v = int(pins.dut.uo_out.value)
    pins.set_uio(1 << CFG_EN, 0)
    await ClockCycles(pins.dut.clk, 3)
    return v


async def configure(pins: Pins, cfg: dict) -> None:
    for addr, data in regs.config_writes(cfg):
        await write_reg(pins, addr, data)


async def drive_adc(pins: Pins, frames, slots: list[int], n_slots: int,
                    slot_cycles: int = SLOT_CYCLES, start_slot: int = 0, seed: int = 0) -> None:
    """frames: iterable of per-frame sample lists, one value per selected slot."""
    rng = random.Random(seed)
    where = {s: i for i, s in enumerate(slots)}
    first = True
    for frame in frames:
        for s in range(start_slot if first else 0, n_slots):
            v = frame[where[s]] if s in where else rng.randrange(1024)
            await pins.strobe(v, frame=int(s == 0))
            await ClockCycles(pins.dut.clk, slot_cycles - 2)
        first = False


async def read_bytes(pins: Pins, n: int, got: list) -> None:
    """Act as the host: the output streams one byte per clock while m_valid (no ack, D8), so
    capture (byte, last) at every falling edge where m_valid is high."""
    dut = pins.dut
    while len(got) < n:
        await FallingEdge(dut.clk)
        if int(dut.m_valid.value):
            got.append((int(dut.m_data.value), int(dut.m_last.value)))


def load_vectors(mode: str) -> tuple[list[list[int]], list[tuple[int, int]], dict]:
    d = VECTORS / mode
    if not d.exists():
        raise FileNotFoundError(f"{d} missing: run `make vectors` first")
    cfg = json.loads((d / "config.json").read_text())
    flat = [int(l, 16) for l in (d / "input.hex").read_text().split()]
    k = len(cfg["slots"])
    frames = [flat[i:i + k] for i in range(0, len(flat), k)]
    words = [int(l, 16) for l in (d / "expected.hex").read_text().split()]
    expected = [(w & 0xFF, (w >> 8) & 1) for w in words if w < 0x200]
    return frames, expected, cfg


async def run_vector_test(dut, mode: str) -> None:
    pins = await reset(dut)
    await configure(pins, load_vectors(mode)[2])
    await run_vectors_on(pins, mode)


async def run_vectors_on(pins: Pins, mode: str) -> None:
    """Drive the vectors into a DUT already configured and enabled, check the bytes."""
    dut = pins.dut
    frames, expected, cfg = load_vectors(mode)
    got: list[tuple[int, int]] = []
    adc = cocotb.start_soon(drive_adc(pins, frames, cfg["slots"], cfg["n_slots"]))
    reader = cocotb.start_soon(read_bytes(pins, len(expected), got))
    await adc.join()
    # all input delivered: the encoder gets a few slot times to flush
    await First(reader.join(), ClockCycles(dut.clk, max(FLUSH_CLOCKS, 200 * SLOT_CYCLES)))
    if not reader.done():
        reader.kill()

    pkt = 0
    for i, ((g, gl), (e, el)) in enumerate(zip(got, expected)):
        assert (g, gl) == (e, el), (
            f"{mode}: packet {pkt}, stream byte {i}: got {g:02x} last={gl}, "
            f"expected {e:02x} last={el}")
        pkt += el
    assert len(got) == len(expected), (
        f"{mode}: received {len(got)}/{len(expected)} bytes "
        f"({pkt}/{cfg['packets']} complete packets) after all input was sent")

    await ClockCycles(dut.clk, 20 * SLOT_CYCLES)
    assert not int(dut.m_valid.value), f"{mode}: extra output after the last packet"
    assert not int(dut.overflow.value), f"{mode}: overflow pin set (a packet was dropped)"
