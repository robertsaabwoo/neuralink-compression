"""nlc_core at the real interface: one slot per clock, 256-slot frames, 5 MHz (D2).

Tests from docs/testing.md 4.3. Environment (ADC, host, scoreboard, monitors): test/env/nlc_env.py.
Every test writes a JSON of what it measured to $NLC_RESULTS (default test/results/core).

  T-IF-1/2/3   interface, slot selection, start-up            (C-IF-1..6)
  T-LAT-1      latency, from the monitors of T-IF-1           (C-LAT-1..3)
  T-BW-1/2     output bandwidth, host turnaround requirement  (C-BW-1..3, C-OVF-5)
  T-OVF-1/2/3  host stalls: abort-and-flush rule (D6) and today's behaviour
  T-ROB-1..7   long run, disable, illegal config, frame faults (D5), power-up state, random,
               rst_n (D7)

Rules (docs/constraints.md D5-D7): frames are delimited by s_frame and a short frame repeats
the missing channels' previous samples; when the output blocks, the packet in flight is aborted
(abort token on m_abort), everything is flushed and output resumes at the next packet boundary;
disable mid-packet sends the abort token; rst_n clears everything.
Known failures on today's RTL (no m_abort port, no padding): docs/results.md F2-F4.

Knobs: NLC_LONG=1 (T-ROB-1 70 packets, T-BW-2 16-packet check, full T-OVF-2 matrix),
NLC_SEEDS=N (T-ROB-6, default 4), NLC_SEED=s (T-ROB-6 single seed, to reproduce).
"""

from __future__ import annotations

import os
import random

import cocotb
from cocotb.handle import ArrayObject

from nlc_env import FLUSH_TAIL, Host, NlcEnv, Stall, codec, frames

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
        f"out FIFO peak {bw.get('out_fifo_peak')}, flush {bw.get('flush_bytes_max')} B | "
        f"latency queue {lat.get('queue_us', {}).get('max')} us, "
        f"processing {lat.get('processing_us', {}).get('max')} us, "
        f"delivery {lat.get('delivery_ms', {}).get('max')} ms")
    for n in env.sb.notes[:10]:
        env.dut._log.info(f"  note: {n}")
    if check:
        assert not env.errors, f"{env.name}: " + "\n".join(env.errors[:10])
    return r


def skip_known() -> None:
    """CI (NLC_SKIP_KNOWN=1) skips tests that fail on today's RTL by design (docs/results.md).
    A runtime skip: cocotb ignores skip=True for tests selected by COCOTB_TEST_FILTER."""
    if os.environ.get("NLC_SKIP_KNOWN") == "1":
        import pytest
        pytest.skip("known failure on today's RTL (NLC_SKIP_KNOWN=1)")


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
    """T-IF-3b: run, disable right after a packet is received, enable: == a run from reset."""
    skip_known()   # F2
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
    """T-BW-1: bytes per frame / packet, out FIFO peak, flush burst for worst and edge data
    (real data: T-IF-1)."""
    source, n = case
    env = await fresh(dut, f"t_bw_1_{source}")
    env.configure(SPREAD)
    env.play_source(source, n)
    await env.run(n)
    report(env, {"source": source})


@cocotb.test(**TIMEOUT)
async def t_bw_2(dut):
    """T-BW-2: slowest host (clocks per byte) with no loss on worst-case (LFSR) data, by
    binary search; then that host is checked for longer. Result = C-BW-3."""
    def ok(r):
        return not r["n_errors"] and not r["overflow"]["lossy_flag"]

    env, lo, hi, trials = None, 1, 64, {}
    while lo < hi:                               # largest T that passes
        t = (lo + hi + 1) // 2
        env = await fresh(dut, f"t_bw_2_T{t}", env, host=Host(turnaround=t))
        env.configure(SPREAD)
        env.play_source("lfsr", 1)
        await env.run(1, timeout_packets=1)
        r = report(env, {"turnaround": t}, check=False)
        trials[t] = ok(r)
        dut._log.info(f"T-BW-2: turnaround {t} clocks/byte -> {'ok' if trials[t] else 'loss'}")
        lo, hi = (t, hi) if trials[t] else (lo, t - 1)
    n = 16 if LONG else 2
    env = await fresh(dut, "t_bw_2", env, host=Host(turnaround=lo))
    env.configure(SPREAD)
    env.play_source("lfsr", n)
    await env.run(n)
    report(env, {"host_turnaround_max_clocks": lo, "host_turnaround_max_us": env.us(lo),
                 "trials": trials, "verified_packets": n})


# ---------------------------------------------------------------------------
# overflow (D3). T-OVF-1/2 state the requirement and fail on today's RTL.
# ---------------------------------------------------------------------------

LAG_CLOCKS = 8 * 256      # a packet's last bytes leave up to ~8 frames after its last frame


def ovf_requirements(env: NlcEnv, stall_clocks: int, n: int) -> list[str]:
    """D6 / C-OVF-1..4 from the pins. Received packets are checked bit-exact by the scoreboard
    (a partial packet without an abort token shows up there as a mismatch). Here:
      - a packet that ended well before the stall is delivered;
      - every packet that starts after the host resumed is delivered (C-OVF-3/4);
      - lost packets overlap the stall (nothing else is thrown away)."""
    seg, out = env.sb.seg, []
    if not env.host.stall_log:
        return ["stall never started"]
    s0 = env.host.stall_log[0][0]
    s1 = s0 + stall_clocks
    for k in range(n):
        f0, f1 = k * FPP, (k + 1) * FPP
        if f1 > len(seg.frame_clock):
            break
        start = seg.frame_clock[f0]
        end = seg.frame_clock[f1 - 1] + 256 + LAG_CLOCKS
        got = k in seg.got
        if end < s0 and not got:
            out.append(f"packet {k} ended before the stall but was not delivered")
        if start > s1 and not got:
            out.append(f"packet {k} starts after the host resumed but was not delivered (C-OVF-3)")
        if not got and not (start <= s1 and end >= s0):
            out.append(f"packet {k} lost although it does not overlap the stall")
    return out


async def _stall_run(dut, name: str, stall: Stall, n: int = 4, check: bool = True) -> dict:
    env = await fresh(dut, name, host=Host(stalls=[stall]), allow_loss=True, allow_abort=True)
    env.configure(SPREAD)
    env.play_source("real", n)
    await env.run(n, timeout_packets=2 + stall.clocks / PKT_CLOCKS)
    env.sb.errors += ovf_requirements(env, stall.clocks, n)
    seg = env.sb.seg
    return report(env, {"stall": vars(stall), "lost": seg.lost,
                        "aborted": [a for a in seg.aborted]}, check=check)


@cocotb.test(**TIMEOUT)
async def t_ovf_1(dut):
    """T-OVF-1: host stops for a whole packet, then resumes. Every packet the host receives
    complete is bit-exact, a packet cut by the stall ends with the abort token, the seq gap
    names the lost packets. C-OVF-1/2 (D6)."""
    await _stall_run(dut, "t_ovf_1", Stall(PKT_CLOCKS, at_packet=1, at_byte=0))


def _ovf2_cases():
    starts = [("header", 0), ("payload", 300), ("flush", None)]
    lens = [64, 4000, PKT_CLOCKS, 3 * PKT_CLOCKS] if LONG else [4000]
    cases = [(w, b, c) for w, b in starts for c in lens]
    return cases if LONG else [("header", 0, PKT_CLOCKS), ("payload", 300, 4000), ("flush", None, 300)]


@cocotb.test(**TIMEOUT)
@cocotb.parametrize(case=_ovf2_cases())
async def t_ovf_2(dut, case):
    """T-OVF-2: stalls of several lengths starting at the header, mid-payload or in the flush;
    every packet that starts after the resume is intact, nothing outside the stall is lost.
    C-OVF-3/4 (D6)."""
    where, byte, clocks = case
    if byte is None:                     # flush: the last FLUSH_TAIL bytes of a typical packet
        byte = 640 - FLUSH_TAIL // 2
    await _stall_run(dut, f"t_ovf_2_{where}_{clocks}", Stall(clocks, at_packet=1, at_byte=byte))


@cocotb.test(**TIMEOUT)
async def t_ovf_3(dut):
    """T-OVF-3: today's behaviour under a 1-packet stall, recorded (the "before" point).
    Passes if it ran; the JSON says what the pins showed."""
    r = await _stall_run(dut, "t_ovf_3", Stall(PKT_CLOCKS, at_packet=1, at_byte=300), check=False)
    dut._log.info(f"T-OVF-3: core overflow pin {r['overflow']['core_pin']}, lossy flag "
                  f"{r['overflow']['lossy_flag']}, {r['n_errors']} scoreboard errors: "
                  f"{r['errors'][:3]}")


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


ROB2_POINTS = ["midframe", "midpacket", "flush", "host_stall"]


@cocotb.test(**TIMEOUT)
@cocotb.parametrize(point=ROB2_POINTS)
async def t_rob_2(dut, point):
    """T-ROB-2: disable at a given point, then enable: the next run == a run from reset."""
    host = Host(stalls=[Stall(3000, at_packet=0, at_byte=200)]) if point == "host_stall" else None
    env = await fresh(dut, f"t_rob_2_{point}", host=host, allow_abort=True)
    env.configure(SPREAD)
    env.play_source("real", 2)
    if point == "midframe":
        await env.until(lambda e: e.data_frame == 10 and e.slot == 100, 20 * 256)
    elif point == "midpacket":
        await env.until(lambda e: e.data_frame == 100 and e.slot == 0, 120 * 256)
    elif point == "flush":
        await env.until(lambda e: e.lossy is not None and int(e.rans.phase.value) != 0, 2 * PKT_CLOCKS)
    else:
        await env.until(lambda e: e.clock in range(e.host.stall_until - 1000, e.host.stall_until),
                        2 * PKT_CLOCKS)
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
    """T-ROB-4: frame faults in packet 0 (early s_frame, late s_frame, missing s_frame).
    Every packet must equal the model on the frames as driven, under the frame rule D5
    (short frame: missing channels repeat their previous sample). C-IF-8."""
    env = await fresh(dut, f"t_rob_4_{fault}")
    env.configure(SPREAD)
    env.play_source("real", 2)
    await env.until(lambda e: e.data_frame == 20, 30 * 256)
    env.frame_faults.extend(FRAME_FAULTS[fault])
    await env.run(2)
    report(env, {"fault": fault, "frame_faults": FRAME_FAULTS[fault]})


NO_RESET = {
    "u_enc.u_lossy": ["e1", "o1", "dp1", "e2", "o2", "dp2", "e3", "o3", "dp3", "qa_prev",
                      "fifo", "fctx", "sbuf", "slast"],
    "u_enc.u_lossy.u_rans": ["st_mem", "a_x", "a_f", "a_c", "a_ch", "a_rv", "a_raw", "d_rem",
                             "d_dq", "d_f", "d_c", "d_ch", "o_data", "o_keep", "o_last"],
    "u_fifo": ["mem"],
}


def _deposit(seed: int):
    def f(dut):
        rng = random.Random(seed)
        n = 0
        for path, names in NO_RESET.items():
            mod = dut
            for p in path.split("."):
                mod = getattr(mod, p)
            for nm in names:
                h = getattr(mod, nm)
                items = list(h) if isinstance(h, ArrayObject) else [h]
                for it in items:
                    it.value = rng.getrandbits(len(it))
                    n += 1
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
    """T-ROB-6: random regression. Each seed picks data source, n_sel, slots and host.
    Reproduce one: make -C test/core COCOTB_TEST_FILTER=t_rob_6 NLC_SEED=<seed>."""
    rng = random.Random(seed)
    source = rng.choice(["real", "real", "synthetic", "lfsr", "ramp", "nyquist", "step"])
    slots = sorted(rng.sample(range(256), rng.randint(1, 8)))
    host = Host(turnaround=rng.choice([1, 1, 2, 4]), p_ready=rng.choice([1.0, 0.9, 0.6]),
                seed=seed)
    dut._log.info(f"T-ROB-6 seed {seed}: {source}, slots {slots}, turnaround "
                  f"{host.turnaround}, p_ready {host.p_ready}  (repro: NLC_SEED={seed})")
    env = await fresh(dut, f"t_rob_6_s{seed}", host=host)
    env.configure(slots)
    env.play_source(source, 1, seed=seed, offset=rng.randrange(0, 90000))
    await env.run(1)
    report(env, {"seed": seed, "source": source, "turnaround": host.turnaround,
                 "p_ready": host.p_ready})


@cocotb.test(**TIMEOUT)
@cocotb.parametrize(point=["midpacket", "flush", "host_stall"])
async def t_rob_7(dut, point):
    """T-ROB-7: rst_n at a given point: the output is empty right after reset, and after
    configure + enable the run equals a run from power-up (D7). The host resets with the chip
    and drops its partial packet."""
    host = Host(stalls=[Stall(3000, at_packet=0, at_byte=200)]) if point == "host_stall" else None
    env = await fresh(dut, f"t_rob_7_{point}", host=host)
    env.configure(SPREAD)
    env.play_source("real", 2)
    if point == "midpacket":
        await env.until(lambda e: e.data_frame == 100, 120 * 256)
    elif point == "flush":
        await env.until(lambda e: e.lossy is not None and int(e.rans.phase.value) != 0, 2 * PKT_CLOCKS)
    else:
        await env.until(lambda e: e.clock in range(e.host.stall_until - 1000, e.host.stall_until),
                        2 * PKT_CLOCKS)
    await env.reset()
    env.play_queue_clear()
    env.configure(SPREAD)
    env.play_source("real", 1, offset=30000)
    await env.run(1)
    report(env)

