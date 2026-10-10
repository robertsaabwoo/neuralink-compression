"""Fault injection at the Tiny Tapeout pins (TT top, test/tb.v): the bring-up flow end to end.

The host does what a board does: reset, write the config through the pins, read it back
(project.v readback, enable = 0), enable, strobe 32-slot frames (1 slot / 2 clocks, real data
in the 8 selected slots, random filler elsewhere) and sample uo_out / uio_out on every clock:
bytes with m_valid, m_last, the abort token (m_last without m_valid), the overflow pin. Then
diagnose.py gets (capture, slots, readback, the full frames driven). Faults sit in the pin
front end and the output register, which the core bench (test_faults.py) does not contain:

  reset synchroniser rst_q[1] stuck-at-0        (the chip held in reset)
  s_strobe synchroniser stb_q[1] stuck-at-0     (an input-sync flop: no strobe reaches the core)
  s_frame capture flop frm_q stuck-at-0         (an input-sync flop: no frame start)
  sample capture flop hi_q[0] stuck-at-1        (sample bit 8)
  output register last_q stuck-at-0, data_q[2] stuck-at-1 (registered outputs)
"""

from __future__ import annotations

import json
import os
import random
import zlib
from pathlib import Path

import cocotb
import numpy as np
from cocotb.handle import Force, Release
from cocotb.triggers import ClockCycles, FallingEdge, ReadOnly

import diagnose as dg
import harness as H
import regs
from nlc_env import frames as make_frames
from test_faults import stick

SLOTS = [1, 5, 6, 12, 17, 22, 30, 31]
N_SLOTS = 32
FPP = dg.FPP
TIMEOUT = dict(timeout_time=10, timeout_unit="sec")
OUT = Path(os.environ.get("NLC_RESULTS", Path(__file__).resolve().parents[1] / "results"
                          / "bringup"))
RB_ADDRS = [regs.CTRL, regs.N_SEL] + [regs.SEL_SLOT + i for i in range(len(SLOTS))]


class PinCapture:
    """The board's capture: every falling edge, uio_out[6] m_valid, [7] m_last, [4] overflow."""

    def __init__(self, dut) -> None:
        self.dut, self.toks, self.on = dut, [], False
        cocotb.start_soon(self._run())

    async def _run(self) -> None:
        d, ovf = self.dut, False
        while True:
            await FallingEdge(d.clk)
            await ReadOnly()
            if not self.on or not d.uio_out.value.is_resolvable:
                continue
            u = int(d.uio_out.value)
            if u >> 6 & 1:
                self.toks.append(("B", int(d.uo_out.value), bool(u >> 7 & 1)))
            elif u >> 7 & 1:
                self.toks.append(("A",))
            if u >> 4 & 1 and not ovf:
                ovf = True
                self.toks.append(("O",))


async def bringup(dut, name: str, n_pk: int = 1, power_up=None) -> tuple[list, dict, np.ndarray]:
    """Reset, config, readback, enable, n_pk packets of frames. -> (capture, readback, frames)"""
    pins = await H.reset(dut)
    u_out = dut.user_project.u_out.q
    u_out.value = 0xA5 if power_up is None else power_up     # power-up content (no reset)
    cap = PinCapture(dut)
    cfg = {"mode": regs.MODE_LOSSY, "slots": SLOTS, "n_slots": N_SLOTS}
    w = regs.config_writes(cfg)
    for a, v in w[:-1]:                                       # everything but enable
        await H.write_reg(pins, a, v)
    rb = {a: await H.read_reg(pins, a) for a in RB_ADDRS}
    await H.write_reg(pins, *w[-1])                           # enable
    fr = make_frames("real", n_pk * FPP, SLOTS, seed=zlib.crc32(name.encode()),
                     n_slots=N_SLOTS)
    cap.on = True
    for row in fr:
        for s in range(N_SLOTS):
            await pins.strobe(int(row[s]), frame=int(s == 0))
            await ClockCycles(dut.clk, H.SLOT_CYCLES - 2)
    # filler frames while the last packet flushes (the chip codes them too: not checked)
    rng = random.Random(1)
    for _ in range(8):
        for s in range(N_SLOTS):
            await pins.strobe(rng.randrange(1024), frame=int(s == 0))
    await ClockCycles(dut.clk, 50)
    return cap.toks, rb, fr


def diagnose(dut, name, toks, rb, fr, fault, check, precision) -> dict:
    d = OUT / name
    d.mkdir(parents=True, exist_ok=True)
    dg.write_capture(d / "capture.txt", toks, f"{name}: {fault}\nslots {SLOTS} (TT pins, "
                     f"{N_SLOTS}-slot frames)")
    np.save(d / "frames.npy", fr)
    (d / "config.json").write_text(json.dumps({"slots": SLOTS, "readback": {
        f"0x{a:02x}": v for a, v in rb.items()}}))
    dia = dg.Diagnoser(SLOTS, fr, readback=rb)
    dia.run(toks)
    rep = dia.report()
    (d / "diagnosis.txt").write_text(rep + "\n")
    v = dia.verdict()
    ok = bool(check(v, dia))
    res = {"name": name, "fault": fault, "verdict": f"[{v.confidence}] {v.text}" if v else
           "no fault found", "kind": v.kind if v else "", "confidence": v.confidence if v else "",
           "block": v.block if v else "", "rtl": [r["rtl"] for r in v.rtl] if v else [],
           "correct": ok, "precision": precision}
    (d / "result.json").write_text(json.dumps(res, indent=1))
    dut._log.info(f"{name}: injected {fault}\n{rep}\n=> {'CORRECT' if ok else 'WRONG'}")
    return res


def names(v) -> str:
    return " ".join(r["rtl"] for r in v.rtl) if v else ""


@cocotb.test(**TIMEOUT)
async def t_clean(dut):
    """No fault, through the pins: readback as written, every packet exact."""
    toks, rb, fr = await bringup(dut, "t_clean", n_pk=2)
    r = diagnose(dut, "t_clean", toks, rb, fr, "none (TT pins)", lambda v, d: v is None, "-")
    assert r["correct"]


@cocotb.test(**TIMEOUT)
async def t_rst_sync(dut):
    """Reset synchroniser: rst_q[1] stuck-at-0, the chip never leaves reset."""
    h = dut.user_project.rst_q
    h.value = Force(1)                       # rst_q = {stuck 0, 1}
    toks, rb, fr = await bringup(dut, "t_rst_sync")
    h.value = Release()
    r = diagnose(dut, "t_rst_sync", toks, rb, fr, "reset synchroniser rst_q[1] stuck-at-0",
                 lambda v, d: kind_in(v, "readback_dead") and "rst_q" in names(v),
                 "front end bounded: rst_q among 5 candidates (rst/strobe/cfg_en sync, "
                 "config port, output register)")
    assert r["correct"]


@cocotb.test(**TIMEOUT)
async def t_stb_sync(dut):
    """s_strobe synchroniser: stb_q[1] stuck-at-0, no strobe edge reaches the core."""
    t = stick(dut.user_project.stb_q, 1, 0)
    toks, rb, fr = await bringup(dut, "t_stb_sync")
    t.cancel()
    r = diagnose(dut, "t_stb_sync", toks, rb, fr, "s_strobe synchroniser stb_q[1] stuck-at-0",
                 lambda v, d: kind_in(v, "readback_dead") and "stb_q" in names(v),
                 "front end bounded: stb_q among 5 candidates")
    assert r["correct"]


@cocotb.test(**TIMEOUT)
async def t_frm_q(dut):
    """s_frame capture flop frm_q stuck-at-0: config works, no frame ever starts."""
    h = dut.user_project.frm_q
    h.value = Force(0)
    toks, rb, fr = await bringup(dut, "t_frm_q")
    h.value = Release()
    r = diagnose(dut, "t_frm_q", toks, rb, fr, "s_frame capture flop frm_q stuck-at-0",
                 lambda v, d: kind_in(v, "no_frames") and "frm_q" in names(v),
                 "frame path bounded: frm_q among 5 candidates (s_frame, slot selector start, "
                 "enable, core clock gate, m_valid)")
    assert r["correct"]


@cocotb.test(**TIMEOUT)
async def t_hi_q(dut):
    """Sample capture flop hi_q[0] (sample bit 8, uio[0]) stuck-at-1."""
    t = stick(dut.user_project.hi_q, 0, 1)
    toks, rb, fr = await bringup(dut, "t_hi_q")
    t.cancel()
    r = diagnose(dut, "t_hi_q", toks, rb, fr, "sample capture hi_q[0] (bit 8) stuck-at-1",
                 lambda v, d: kind_in(v, "shared_stuck") and ("x0", 8, 1) in v.data["candidates"],
                 "sample bit 8 stuck-at-1 (x0 = pins / ui_q / hi_q / u_smp, equivalents listed)")
    assert r["correct"]


@cocotb.test(**TIMEOUT)
async def t_last_q(dut):
    """Output register: last_q stuck-at-0 (m_last never reaches uio[7])."""
    h = dut.user_project.last_q
    h.value = Force(0)
    toks, rb, fr = await bringup(dut, "t_last_q")
    h.value = Release()
    r = diagnose(dut, "t_last_q", toks, rb, fr, "output register last_q stuck-at-0",
                 lambda v, d: kind_in(v, "no_last") and "last_q" in names(v),
                 "m_last path: output register / uio[7]")
    assert r["correct"]


@cocotb.test(**TIMEOUT)
async def t_data_q(dut):
    """Output register: data_q[2] (u_out.q) stuck-at-1."""
    t = stick(dut.user_project.u_out.q, 2, 1)
    toks, rb, fr = await bringup(dut, "t_data_q")
    t.cancel()
    r = diagnose(dut, "t_data_q", toks, rb, fr, "output register data_q[2] stuck-at-1",
                 lambda v, d: kind_in(v, "pin_stuck") and v.data == {"bit": 2, "value": 1}
                 and "u_out" in names(v),
                 "uo_out[2] / output register bit 2, polarity (readback shows the same bit)")
    assert r["correct"]


def kind_in(v, *ks) -> bool:
    return v is not None and v.kind in ks
