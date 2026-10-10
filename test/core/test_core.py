"""nlc_core at the real interface: one slot per clock, 256-slot frames, 5 MHz (D2).

Tests from docs/testing.md 4.3. Environment (ADC, host, scoreboard, monitors): test/env/nlc_env.py.
Every test writes a JSON of what it measured to $NLC_RESULTS (default test/results/core).

  T-IF-1/2/3   interface, slot selection, start-up            (C-IF-1..6)
  T-LAT-1      latency, from the monitors of T-IF-1           (C-LAT-1..3)
  T-BW-1       output bandwidth                               (C-BW-1/2)
  T-ROB-1..7   long run, disable, illegal config, frame faults (D5), power-up state, random,
               rst_n (D7)

Rules (docs/constraints.md D5-D8): frames are delimited by s_frame; a short frame aborts the
packet holding it (abort token on m_abort), output resumes at the next packet boundary; disable
mid-packet sends the abort token; rst_n clears everything. The output is a valid-only stream
(D8): the host takes a byte in every clock with m_valid, it cannot stall the output.

Retired by D8 (no back-pressure, valid-only streaming), docs/testing.md: T-BW-2 (host
turnaround), T-OVF-1/2/3 (blocked output), the host_stall cases of T-ROB-2 and T-ROB-7, and
T-ROB-6's random host (turnaround, p_ready).

Knobs: NLC_LONG=1 (T-ROB-1 70 packets), NLC_SEEDS=N (T-ROB-6, default 4), NLC_SEED=s (T-ROB-6
single seed, to reproduce).
"""

from __future__ import annotations

import os
import random

import cocotb
from cocotb.handle import ArrayObject

from nlc_env import NlcEnv, codec, frames

SPREAD = [3, 40, 41, 90, 128, 200, 254, 255]
FPP = codec().cfg.frames_per_packet
PKT_CLOCKS = FPP * 256
LONG = os.environ.get("NLC_LONG") == "1"
TIMEOUT = dict(timeout_time=5, timeout_unit="sec")   # simulated time: ~380 packets


def report(env: NlcEnv, extra: dict | None = None, check: bool = True) -> dict:
    r = env.write_report(extra)
    bw = r.get("bandwidth", {})
    lat = r.get("latency", {})
    env.dut._log.info(
        f"{env.name}: {r['packets_received']} packets, {r.get('bits_per_sample', 0):.3f} b/s, "
        f"{r['n_errors']} errors | bytes/frame max {bw.get('bytes_per_frame_max')}, "
        f"flush {bw.get('flush_bytes_max')} B | "
        f"latency queue {lat.get('queue_us', {}).get('max')} us, "
        f"processing {lat.get('processing_us', {}).get('max')} us, "
        f"delivery {lat.get('delivery_ms', {}).get('max')} ms")
    for n in env.sb.notes[:10]:
        env.dut._log.info(f"  note: {n}")
    if check:
        assert not env.errors, f"{env.name}: " + "\n".join(env.errors[:10])
    return r


async def fresh(dut, name: str, prev: NlcEnv | None = None, **kw) -> NlcEnv:
    """New environment on a freshly reset DUT (one clock per cocotb test)."""
    if prev is not None:
        prev.stop()
    env = NlcEnv(dut, name, **kw)
    await env.start(clock=prev is None)
    return env


# ---------------------------------------------------------------------------
# interface
# ---------------------------------------------------------------------------

@cocotb.test(**TIMEOUT)
async def t_if_1(dut):
    """T-IF-1 (+ T-LAT-1): 8 spread slots of 256, one slot per clock, real data, 4 packets."""
    env = await fresh(dut, "t_if_1")
    env.configure(SPREAD)
    env.play_source("real", 4)
    await env.run(4)
    report(env)


def _slot_sets() -> list[tuple[str, list[int]]]:
    rng = random.Random(42)
    sets = [("slot0", [0]), ("slot255", [255]), ("low8", list(range(8))),
            ("high8", list(range(248, 256))), ("ends", [0, 255])]
    for n in range(2, 8):
        sets.append((f"rand{n}", sorted(rng.sample(range(256), n))))
    return sets


@cocotb.test(**TIMEOUT)
@cocotb.parametrize(case=_slot_sets())
async def t_if_2(dut, case):
    """T-IF-2: slot selection sweep (n_sel 1..8, slots 0/255/adjacent/random), 1 packet each."""
    name, slots = case
    env = await fresh(dut, f"t_if_2_{name}")
    env.configure(slots)
    env.play_source("real", 1)
    await env.run(1)
    report(env, {"case": name})


@cocotb.test(**TIMEOUT)
async def t_if_3_enable_midframe(dut):
    """T-IF-3a: enable lands mid-frame: the first packet starts at the next s_frame, seq 0."""
    env = await fresh(dut, "t_if_3_enable_midframe")
    env.configure(SPREAD, enable=False)
    await env.until(lambda e: not e.cfg_q and e.slot == 100, 2 * 256)
    env.enable()
    env.play_source("real", 1)
    await env.run(1)
    report(env)


@cocotb.test(**TIMEOUT)
async def t_if_3_reenable(dut):
    """T-IF-3b: run, disable right after a packet is received, enable: == a run from reset.
    Passes since the eager FIFO drain: no byte of the next packet is out yet at that point
    (disabling mid-packet is still F2: T-ROB-2/3)."""
    env = await fresh(dut, "t_if_3_reenable", allow_abort=True)
    env.configure(SPREAD)
    env.play_source("real", 1)
    await env.run(1)
    env.disable()
    await env.idle(300)
    env.enable()
    env.play_source("real", 1, offset=20000)
    await env.run(1)
    report(env)


# ---------------------------------------------------------------------------
# bandwidth
# ---------------------------------------------------------------------------

BW_SOURCES = [("lfsr", 2), ("zeros", 1), ("full", 1), ("nyquist", 1), ("step", 1), ("ramp", 1)]


@cocotb.test(**TIMEOUT)
@cocotb.parametrize(case=BW_SOURCES)
async def t_bw_1(dut, case):
    """T-BW-1: bytes per frame / packet, flush burst for worst and edge data
    (real data: T-IF-1)."""
    source, n = case
    env = await fresh(dut, f"t_bw_1_{source}")
    env.configure(SPREAD)
    env.play_source(source, n)
    await env.run(n)
    report(env, {"source": source})


BW3_SLOTS = {"end": list(range(248, 256)), "start": list(range(8))}
BW3_SOURCES = [("lfsr", 2), ("nyquist", 1), ("full", 1)]


@cocotb.test(**TIMEOUT)
@cocotb.parametrize(slots=list(BW3_SLOTS), case=BW3_SOURCES)
async def t_bw_3(dut, slots, case):
    """T-BW-3 (C-IF-9, C-OVF-5): II=1, 256-slot frames, worst-case data on 8 adjacent channels
    (the tightest placement of scripts/proofs/output_bound.py): no abort, bit-exact."""
    source, n = case
    env = await fresh(dut, f"t_bw_3_{slots}_{source}")
    env.configure(BW3_SLOTS[slots])
    env.play_source(source, n)
    await env.run(n)
    report(env, {"source": source, "slots": slots})


# T-BW-2 (slowest host turnaround, C-BW-3) and T-OVF-1/2/3 (host stalls: blocked output,
# C-OVF-1..5) are retired by D8: the output has no back-pressure, a host that stalls it does
# not exist (docs/constraints.md D8, docs/testing.md).


# ---------------------------------------------------------------------------
# robustness
# ---------------------------------------------------------------------------

@cocotb.test(skip=not LONG, **TIMEOUT)
async def t_rob_1(dut):
    """T-ROB-1 (NLC_LONG=1): 70 packets of generator LFSR data; seq wraps 63 -> 0."""
    env = await fresh(dut, "t_rob_1")
    env.configure(SPREAD)
    env.play_source("lfsr", 70)
    await env.run(70)
    r = report(env)
    assert r["coverage"].get("seq_wrap", 0) >= 1, "seq did not wrap"


ROB2_POINTS = ["midframe", "midpacket", "flush"]   # host_stall: retired by D8


@cocotb.test(**TIMEOUT)
@cocotb.parametrize(point=ROB2_POINTS)
async def t_rob_2(dut, point):
    """T-ROB-2: disable at a given point, then enable: the next run == a run from reset."""
    env = await fresh(dut, f"t_rob_2_{point}", allow_abort=True)
    env.configure(SPREAD)
    env.play_source("real", 2)
    if point == "midframe":
        await env.until(lambda e: e.data_frame == 10 and e.slot == 100, 20 * 256)
    elif point == "midpacket":
        await env.until(lambda e: e.data_frame == 100 and e.slot == 0, 120 * 256)
    else:
        await env.until(lambda e: e.lossy is not None and int(e.rans.phase.value) != 0, 2 * PKT_CLOCKS)
    env.disable()
    await env.idle(300)
    env.play_queue_clear()
    env.enable()
    env.play_source("real", 1, offset=30000)
    await env.run(1)
    report(env)


@cocotb.test(**TIMEOUT)
async def t_rob_3(dut):
    """T-ROB-3: config written while enabled (illegal): no hang; after disable, reconfigure,
    enable the output is correct. C-IF-7."""
    env = await fresh(dut, "t_rob_3", allow_abort=True)
    env.configure(SPREAD)
    env.play_source("real", 1)
    await env.until(lambda e: e.data_frame == 50, 60 * 256)
    env.write(0x01, 3)                      # n_sel while running
    env.write(0x10 + 1, 2)                  # slot 1 below slot 0: not ascending
    await env.idle(20 * 256)
    env.disable()
    env.play_queue_clear()
    env.configure(SPREAD)
    env.play_source("real", 1, offset=30000)
    await env.run(1)
    report(env)


FRAME_FAULTS = {
    "short": [("short", 200)],              # cuts the last 3 selected slots (200, 254, 255)
    "short_early": [("short", 20)],         # only slot 3 reached
    "short_twice": [("short", 100), ("short", 100)],
    "long": [("long", 300)],
    "missing": [("missing",)],
}


@cocotb.test(**TIMEOUT)
@cocotb.parametrize(fault=list(FRAME_FAULTS))
async def t_rob_4(dut, fault):
    """T-ROB-4: frame faults in packet 0 (early s_frame, late s_frame, missing s_frame), D5.
    A short frame is a fault: the packet holding it is aborted (abort token, seq gap) and the
    next packet is bit-exact against the model on the frames as driven (the short frame counts
    as one frame). Late or missing s_frame: every packet bit-exact (extra slots are ignored, a
    missing s_frame merges two frames). C-IF-8."""
    short = any(f[0] == "short" for f in FRAME_FAULTS[fault])
    n = 3 if short else 2
    env = await fresh(dut, f"t_rob_4_{fault}", allow_loss=short, allow_abort=short)
    env.configure(SPREAD)
    env.play_source("real", n)
    await env.until(lambda e: e.data_frame == 20, 30 * 256)
    env.frame_faults.extend(FRAME_FAULTS[fault])
    await env.run(n)
    seg = env.sb.seg
    if short:
        if 0 in seg.got:
            env.sb.errors.append("packet 0 holds a short frame but was delivered (D5: abort)")
        if 1 not in seg.got:
            env.sb.errors.append("packet 1, after the short frame, was not delivered")
    report(env, {"fault": fault, "frame_faults": FRAME_FAULTS[fault]})


# Registers without reset that are not clock-gated storage (nlc_greg instances are found
# by walking the hierarchy: every nlc_greg holds no-reset storage by design). The looped
# rANS divider (nlc_rans, DIV_K) keeps its loop register x_l in an nlc_greg and resets all
# of its control registers, so nothing outside the gated storage is listed today.
NO_RESET: dict[str, list[str]] = {}


def _gated_regs(h, depth: int = 0) -> list:
    """The q of every nlc_greg below h (a scope with the ports clk, en, d, q; RTL sim
    lists the parameter W too, synthesis-style builds the gate gclk, u_icg)."""
    found = []
    if depth > 8:
        return found
    for c in h:
        try:
            kids = {k._name for k in c}
        except TypeError:                 # a signal, not a scope
            continue
        if {"clk", "en", "d", "q"} <= kids <= {"clk", "en", "d", "q", "W", "gclk", "u_icg"}:
            found.append(c.q)
        else:
            found += _gated_regs(c, depth + 1)
    return found


def _deposit(seed: int):
    def f(dut):
        rng = random.Random(seed)
        regs = _gated_regs(dut)
        for path, names in NO_RESET.items():
            mod = dut
            for p in path.split("."):
                mod = getattr(mod, p)
            regs += [getattr(mod, nm) for nm in names]
        n = 0
        for h in regs:
            for it in (list(h) if isinstance(h, ArrayObject) else [h]):
                it.value = rng.getrandbits(len(it))
                n += 1
        assert n > 100, f"T-ROB-5 found only {n} no-reset registers: hierarchy walk broken?"
        dut._log.info(f"T-ROB-5: random power-up state in {n} registers (seed {seed})")
    return f


@cocotb.test(**TIMEOUT)
@cocotb.parametrize(seed=[1, 2, 3])
async def t_rob_5(dut, seed):
    """T-ROB-5: random power-up contents in every register without reset: output identical
    to the model (Icarus X-init is covered by every other test). C-FN-6."""
    env = NlcEnv(dut, f"t_rob_5_s{seed}")
    await env.start(before_reset=_deposit(seed))
    env.configure(SPREAD)
    env.play_source("real", 1)
    await env.run(1)
    report(env)


def _seeds() -> list[int]:
    if os.environ.get("NLC_SEED"):
        return [int(os.environ["NLC_SEED"])]
    return list(range(1, int(os.environ.get("NLC_SEEDS", "4")) + 1))


@cocotb.test(**TIMEOUT)
@cocotb.parametrize(seed=_seeds())
async def t_rob_6(dut, seed):
    """T-ROB-6: random regression. Each seed picks data source, n_sel and slots (the host
    takes every byte, D8).
    Reproduce one: make -C test/core COCOTB_TEST_FILTER=t_rob_6 NLC_SEED=<seed>."""
    rng = random.Random(seed)
    source = rng.choice(["real", "real", "synthetic", "lfsr", "ramp", "nyquist", "step"])
    slots = sorted(rng.sample(range(256), rng.randint(1, 8)))
    dut._log.info(f"T-ROB-6 seed {seed}: {source}, slots {slots}  (repro: NLC_SEED={seed})")
    env = await fresh(dut, f"t_rob_6_s{seed}")
    env.configure(slots)
    env.play_source(source, 1, seed=seed, offset=rng.randrange(0, 90000))
    await env.run(1)
    report(env, {"seed": seed, "source": source})


@cocotb.test(**TIMEOUT)
@cocotb.parametrize(point=["midpacket", "flush"])   # host_stall: retired by D8
async def t_rob_7(dut, point):
    """T-ROB-7: rst_n at a given point: the output is empty right after reset, and after
    configure + enable the run equals a run from power-up (D7). The host resets with the chip
    and drops its partial packet."""
    env = await fresh(dut, f"t_rob_7_{point}")
    env.configure(SPREAD)
    env.play_source("real", 2)
    if point == "midpacket":
        await env.until(lambda e: e.data_frame == 100, 120 * 256)
    else:
        await env.until(lambda e: e.lossy is not None and int(e.rans.phase.value) != 0, 2 * PKT_CLOCKS)
    await env.reset()
    env.play_queue_clear()
    env.configure(SPREAD)
    env.play_source("real", 1, offset=30000)
    await env.run(1)
    report(env)



# ---------------------------------------------------------------------------
# power scenarios (T-PWR-2): the whole core (config, slot selector, encoder)
# at the real interface. Gate level with a VCD in scripts/flow/flow.py (step power);
# in RTL they are ordinary bit-exact runs.
# ---------------------------------------------------------------------------

@cocotb.test(**TIMEOUT)
async def t_pwr_op(dut):
    """T-PWR-2 op: 8 spread slots of 256, real data, 2 packets (VCD from frame 64)."""
    env = await fresh(dut, "t_pwr_op")
    env.configure(SPREAD)
    env.play_source("real", 2)
    await env.run(2)
    report(env)


@cocotb.test(**TIMEOUT)
async def t_pwr_idle(dut):
    """T-PWR-2 idle: configured, enable = 0, the ADC stream keeps running (C-PWR-2)."""
    env = await fresh(dut, "t_pwr_idle")
    env.configure(SPREAD, enable=False)
    await env.idle(4 * 256)
    assert int(dut.m_valid.value) == 0
    report(env)
