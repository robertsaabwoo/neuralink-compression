"""Fault injection: does scripts/bringup/diagnose.py name the faulty block from the bytes alone?

Each test resets nlc_core (inside the test-only wrapper nlc_fault_top.v), injects one fault,
plays real data (8 spread slots of 256, one slot per clock, the real interface), captures the
output exactly as the board's host would (bytes, m_last, abort tokens), then runs the
diagnoser on (capture, slots, the frames that were driven) and checks its verdict against the
injected fault. Faults:

  (a) row never written (dead clock gate): cocotb Force on a g_ch[c] row's q
  (b) stuck-at bit on a shared net: lifter outputs u_l2.d / u_l1.a (wrapper force hook)
  (c) rANS state bit flip: one Deposit on u_rans.g_st[c].u_st.q mid-packet;
      a ROM entry bit flipped, a write-back bit stuck (wrapper hooks)
  (d) slot selector: a channel compares against the wrong slot; two channels swapped
  (e) pin-level byte errors: applied to the captured stream (what the board would see)
  (f) header: seq bit stuck (serialiser output hook), seq register upset (Deposit)
  controls: no fault; host stall (abort token + seq gap, a protocol event, not a fault)

Per fault: test/results/bringup/<name>/{capture.txt, frames.npy, config.json, diagnosis.txt,
result.json}; scripts/bringup/fault_table.py makes the summary table.
"""

from __future__ import annotations

import json
import os
import zlib
from pathlib import Path

import cocotb
import numpy as np
from cocotb.handle import Force, Release

import diagnose as dg
from nlc_env import Host, NlcEnv, Stall

SPREAD = [3, 40, 41, 90, 128, 200, 254, 255]
TIMEOUT = dict(timeout_time=5, timeout_unit="sec")
OUT = Path(os.environ.get("NLC_RESULTS", Path(__file__).resolve().parents[1] / "results"
                          / "bringup"))
_CLEAN: dict = {}                      # the clean capture, reused for the pin-level faults


class Capture:
    """The host's view: every byte with m_last, abort tokens, in order."""

    def __init__(self, env: NlcEnv) -> None:
        self.toks: list[tuple] = []
        sb = env.sb
        on_packet, on_abort = sb.on_packet, sb.on_abort

        def pkt(p, clock):
            self.toks += [("B", b, i == len(p) - 1) for i, b in enumerate(p)]
            on_packet(p, clock)

        def abort(partial, clock):
            self.toks += [("B", b, False) for b in partial] + [("A",)]
            on_abort(partial, clock)
        sb.on_packet, sb.on_abort = pkt, abort
        self.env = env

    def finish(self) -> list[tuple]:
        return self.toks + [("B", b, False) for b in self.env.rx]


async def start(dut, name: str, **kw) -> tuple[NlcEnv, Capture]:
    dut.fault_sel.value = 0
    env = NlcEnv(dut, name, monitors=False, **kw)
    await env.start(clock=True)             # cocotb ends a test's tasks: one clock per test
    power_up(dut, name)
    return env, Capture(env)


def power_up(dut, name: str) -> None:
    """The per-channel rows have no reset (written before read in normal operation). On
    silicon they power up random; in Icarus they are X, and a fault that reads one before it
    is written (f_d_swap) would push X into the coder. Deposit random power-up contents."""
    rng = np.random.default_rng(zlib.crc32(name.encode()))
    for c in range(8):
        for r, w in dg.datapath.ROW_W.items():
            getattr(lossy(dut).g_ch[c], f"u_{r}").q.value = int(rng.integers(0, 1 << w))


def lossy(dut):
    return dut.core.u_enc.u_lossy


def diagnose(name: str, env: NlcEnv, toks, fault: str, check, precision: str,
             slots=SPREAD) -> dict:
    seg = env.sb.segments[0]
    frames = np.array([np.asarray(r) for r in seg.frames], dtype=np.int64)
    d = OUT / name
    d.mkdir(parents=True, exist_ok=True)
    dg.write_capture(d / "capture.txt", toks, f"{name}: {fault}\nslots {slots}")
    np.save(d / "frames.npy", frames)
    (d / "config.json").write_text(json.dumps({"slots": slots}))
    dia = dg.Diagnoser(slots, frames)
    dia.run(toks)
    rep = dia.report()
    (d / "diagnosis.txt").write_text(rep + "\n")
    v = dia.verdict()
    ok = bool(check(v, dia))
    res = {"name": name, "fault": fault, "verdict": f"[{v.confidence}] {v.text}" if v else
           "no fault found", "block": v.block if v else "", "rtl": [r["rtl"] for r in v.rtl]
           if v else [], "correct": ok, "precision": precision}
    (d / "result.json").write_text(json.dumps(res, indent=1))
    env.dut._log.info(f"{name}: injected {fault}\n{rep}\n=> {'CORRECT' if ok else 'WRONG'}")
    return res


def kind(v, k, **data) -> bool:
    return v is not None and v.kind == k and all(v.data.get(a) == b for a, b in data.items())


async def run_real(env: NlcEnv, n: int, slots=SPREAD, offset: int = 5000) -> None:
    env.configure(slots)
    env.play_source("real", n, offset=offset)
    await env.run(n)


# ---------------------------------------------------------------------------
# controls
# ---------------------------------------------------------------------------

@cocotb.test(**TIMEOUT)
async def f_clean(dut):
    """No fault: the tool must find nothing."""
    env, cap = await start(dut, "f_clean")
    await run_real(env, 2)
    toks = cap.finish()
    _CLEAN.update(toks=toks, frames=env.sb.segments[0].frames)
    r = diagnose("f_clean", env, toks, "none", lambda v, d: v is None, "-")
    assert r["correct"]


@cocotb.test(**TIMEOUT)
async def f_stall(dut):
    """Host stalls mid-packet: abort token + seq gap = a protocol event, not a hardware fault."""
    env, cap = await start(dut, "f_stall", host=Host(stalls=[Stall(3000, at_packet=0,
                                                                   at_byte=200)]),
                           allow_loss=True, allow_abort=True)
    await run_real(env, 2)
    r = diagnose("f_stall", env, cap.finish(), "host stall 3000 clocks at byte 200 (control)",
                 lambda v, d: v is not None and v.kind in ("seq_gap", "abort")
                 and not any(f.kind in ("row_stuck", "shared_stuck", "coder", "state_flip",
                                        "header", "symbols_differ") for f in d.findings),
                 "abort token + lost packet 0 named; no fault claimed")
    assert r["correct"]


# ---------------------------------------------------------------------------
# (a) per-channel row never written
# ---------------------------------------------------------------------------

async def _row(dut, name, ch, row, value, desc):
    env, cap = await start(dut, name)
    h = getattr(lossy(dut).g_ch[ch], f"u_{row}").q
    h.value = Force(value)
    await run_real(env, 1)
    h.value = Release()
    r = diagnose(name, env, cap.finish(), desc,
                 lambda v, d: kind(v, "row_stuck", row=row, channel=ch),
                 f"channel {ch}, row {row}, and its power-up value")
    assert r["correct"]


@cocotb.test(**TIMEOUT)
async def f_a_dp2(dut):
    """(a) channel 3's level-2 dp row never written (its clock gate dead), holds 0x5a5."""
    await _row(dut, "f_a_dp2", 3, "dp2", 0x5A5, "g_ch[3].u_dp2.q stuck at 0x5a5 (dead gate)")


@cocotb.test(**TIMEOUT)
async def f_a_o1(dut):
    """(a) channel 5's level-1 odd row never written, holds 0x123."""
    await _row(dut, "f_a_o1", 5, "o1", 0x123, "g_ch[5].u_o1.q stuck at 0x123 (dead gate)")


@cocotb.test(**TIMEOUT)
async def f_a_b3(dut):
    """(a) channel 0's burst slot 3 (a3 delta) never written, holds 0x0007."""
    await _row(dut, "f_a_b3", 0, "b3", 0x7, "g_ch[0].u_b3.q stuck at 7 (dead gate)")


@cocotb.test(**TIMEOUT)
async def f_a_gclk(dut):
    """(a) the hardware fault itself: g_ch[3].u_dp2's clock gate output stuck low (only with
    ICG_SIM=yes, where nlc_greg instantiates the gate). Row powers up at 0x2c3."""
    if os.environ.get("NLC_ICG_SIM") != "1":
        dut._log.info("f_a_gclk needs ICG_SIM=yes (real clock gates in nlc_greg): skipped")
        return
    env, cap = await start(dut, "f_a_gclk")
    g = lossy(dut).g_ch[3].u_dp2
    g.q.value = 0x2C3
    g.gclk.value = Force(0)
    await run_real(env, 1)
    g.gclk.value = Release()
    r = diagnose("f_a_gclk", env, cap.finish(), "g_ch[3].u_dp2 GCLK forced 0 (CLK pin open)",
                 lambda v, d: kind(v, "row_stuck", row="dp2", channel=3),
                 "channel 3, row dp2, its clock gate cell named")
    assert r["correct"]


# ---------------------------------------------------------------------------
# (b) shared datapath stuck-at
# ---------------------------------------------------------------------------

async def _shared(dut, name, sel, bit, sa, sig, desc):
    env, cap = await start(dut, name)
    dut.fault_bit.value, dut.fault_sa.value = bit, sa
    dut.fault_sel.value = sel
    await run_real(env, 1)
    dut.fault_sel.value = 0
    r = diagnose(name, env, cap.finish(), desc,
                 lambda v, d: v is not None and v.kind == "shared_stuck"
                 and (sig, bit, sa) in v.data["candidates"],
                 f"net {sig} and the bit (equivalent candidates listed)")
    assert r["correct"]


@cocotb.test(**TIMEOUT)
async def f_b_d2(dut):
    """(b) level-2 lifter detail output u_l2.d bit 3 stuck-at-1 (all channels)."""
    await _shared(dut, "f_b_d2", 1, 3, 1, "d2", "u_l2.d[3] stuck-at-1")


@cocotb.test(**TIMEOUT)
async def f_b_a1(dut):
    """(b) level-1 lifter approximation u_l1.a bit 5 stuck-at-0 (all channels)."""
    await _shared(dut, "f_b_a1", 2, 5, 0, "a1", "u_l1.a[5] stuck-at-0")


# ---------------------------------------------------------------------------
# (c) rANS state bit flip
# ---------------------------------------------------------------------------

@cocotb.test(**TIMEOUT)
async def f_c_state(dut):
    """(c) channel 4's rANS state, bit 17 flipped once mid-packet (frame 100)."""
    env, cap = await start(dut, "f_c_state")
    env.configure(SPREAD)
    env.play_source("real", 1)
    await env.until(lambda e: e.data_frame == 100 and e.slot == 20, 200 * 256)
    h = lossy(dut).u_rans.g_st[4].u_st.q
    h.value = int(h.value) ^ (1 << 17)
    await env.run(1)
    r = diagnose("f_c_state", env, cap.finish(), "g_st[4].u_st.q bit 17 flipped at frame 100",
                 lambda v, d: kind(v, "state_flip", channel=4, bit=17),
                 "channel 4, state bit 17, the symbol it happened before (frame 100)")
    assert r["correct"]


@cocotb.test(**TIMEOUT)
async def f_c_rom(dut):
    """(c') rANS ROM: entry d1 / symbol 31 (value 0, the commonest), c_s bit 4 flipped."""
    env, cap = await start(dut, "f_c_rom")
    dut.fault_addr.value = (1 << 6) | 31
    dut.fault_bit.value = 4
    dut.fault_sel.value = 6
    await run_real(env, 1)
    dut.fault_sel.value = 0
    r = diagnose("f_c_rom", env, cap.finish(), "ROM entry (d1, sym 31): in_fc bit 4 (c_s[4]) flipped",
                 lambda v, d: v is not None and v.kind == "rom"
                 and any(c[1:5] == (1, 31, "c", 4) for c in v.data["candidates"]),
                 "ROM entry (context, symbol), field and bit")
    assert r["correct"]


@cocotb.test(**TIMEOUT)
async def f_c_wb(dut):
    """(c') rANS write-back wb_x bit 9 stuck-at-1 (shared divider/adder output)."""
    env, cap = await start(dut, "f_c_wb")
    dut.fault_bit.value, dut.fault_sa.value = 9, 1
    dut.fault_sel.value = 7
    await run_real(env, 1)
    dut.fault_sel.value = 0
    r = diagnose("f_c_wb", env, cap.finish(), "u_rans.wb_x[9] stuck-at-1",
                 lambda v, d: v is not None and v.kind == "coder_wb"
                 and ("wb", 9, 1) in v.data["candidates"],
                 "the write-back bit and polarity")
    assert r["correct"]


# ---------------------------------------------------------------------------
# (d) slot selector
# ---------------------------------------------------------------------------

@cocotb.test(**TIMEOUT)
async def f_d_slot(dut):
    """(d) channel 2 is taken from slot 42 instead of 41 (selector compare off by one)."""
    env, cap = await start(dut, "f_d_slot")
    dut.fault_ch_a.value = 2
    dut.fault_sel.value = 4
    await run_real(env, 1)
    dut.fault_sel.value = 0
    r = diagnose("f_d_slot", env, cap.finish(), "u_sel.want + 1 for channel 2 (slot 42)",
                 lambda v, d: kind(v, "slot") and v.data["channels"] == {2: [42]},
                 "channel 2 and the slot it actually carries")
    assert r["correct"]


@cocotb.test(**TIMEOUT)
async def f_d_swap(dut):
    """(d) channels 1 and 6 swapped in the slot selector's sample register (smp_ch). Channel 6
    gets slot 40; channel 1 gets slot 254, but its row is written after the issuer read it
    (the issuer reaches channel 1 right after slot 40), so it codes one frame late."""
    env, cap = await start(dut, "f_d_swap")
    dut.fault_ch_a.value, dut.fault_ch_b.value = 1, 6
    dut.fault_sel.value = 3
    await run_real(env, 1)
    dut.fault_sel.value = 0
    r = diagnose("f_d_swap", env, cap.finish(), "smp_ch 1 <-> 6 swapped",
                 lambda v, d: kind(v, "slot") and v.confidence == "exact"
                 and v.data["channels"] == {1: ["254 (late)"], 6: [40]},
                 "both channels, the slots they carry, 'channel index/order'")
    assert r["correct"]


# ---------------------------------------------------------------------------
# (e) pin-level byte errors (on the clean RTL capture)
# ---------------------------------------------------------------------------

def _pin_case(dut, name, toks, desc, check, precision):
    class E:                                     # what diagnose() needs from an env
        pass
    e = E()
    e.dut = dut
    e.sb = type("S", (), {"segments": [type("G", (), {"frames": _CLEAN["frames"]})()]})()
    r = diagnose(name, e, toks, desc, check, precision)
    assert r["correct"]


@cocotb.test(**TIMEOUT)
async def f_e_pins(dut):
    """(e) on the clean capture: a dropped byte, a corrupted byte, uo_out[5] stuck-at-0."""
    if not _CLEAN:
        raise RuntimeError("needs f_clean first")
    toks = list(_CLEAN["toks"])
    def pkt_of(i):
        return sum(1 for x in toks[:i] if x[0] == "B" and x[2])
    t = list(toks)
    del t[777]
    _pin_case(dut, "f_e_drop", t, f"stream byte 777 (packet {pkt_of(777)}) dropped",
              lambda v, d: kind(v, "byte_drop") and v.packet == pkt_of(777),
              "packet and byte index")
    t = list(toks)
    i = len(toks) - 300                          # in packet 1
    t[i] = ("B", t[i][1] ^ 0x04, t[i][2])
    _pin_case(dut, "f_e_corrupt", t, f"bit 2 of stream byte {i} flipped",
              lambda v, d: v is not None and v.kind == "byte_corrupt" and v.packet == pkt_of(i),
              "packet, byte index and bit (or 'ambiguous' with a state flip)")
    t = [(x[0], x[1] & ~0x20, x[2]) if x[0] == "B" else x for x in toks]
    _pin_case(dut, "f_e_pin5", t, "uo_out[5] stuck-at-0",
              lambda v, d: kind(v, "pin_stuck", bit=5, value=0), "the pin and the polarity")


# ---------------------------------------------------------------------------
# (f) header / seq
# ---------------------------------------------------------------------------

@cocotb.test(**TIMEOUT)
async def f_f_seqbit(dut):
    """(f) header: seq bit 1 stuck-at-1 at the serialiser output."""
    env, cap = await start(dut, "f_f_seqbit")
    dut.fault_bit.value, dut.fault_sa.value = 1, 1
    dut.fault_sel.value = 5
    await run_real(env, 2)
    dut.fault_sel.value = 0
    r = diagnose("f_f_seqbit", env, cap.finish(), "header seq bit 1 stuck-at-1",
                 lambda v, d: kind(v, "seq_bit") and v.data["xor"] == [0x02],
                 "header seq, the bit (payload identified as the right packet)")
    assert r["correct"]


@cocotb.test(**TIMEOUT)
async def f_f_sequpset(dut):
    """(f) the seq register jumps by +5 after packet 0 (an upset): later headers are off."""
    env, cap = await start(dut, "f_f_sequpset")
    env.configure(SPREAD)
    env.play_source("real", 3)
    await env.until(lambda e: e.sb.packets >= 1, 3 * 256 * 256)
    s = lossy(dut).seq
    s.value = (int(s.value) + 5) & 63
    await env.run(3)
    r = diagnose("f_f_sequpset", env, cap.finish(), "seq register +5 after packet 0",
                 lambda v, d: kind(v, "seq_upset") and v.packet == 1
                 and v.data["offset"] == [5],
                 "header seq of packets 1, 2; constant offset = register upset")
    assert r["correct"]
