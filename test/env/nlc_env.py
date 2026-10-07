"""Test environment for nlc_core: the platform around the chip (docs/testing.md 4.1).

One coroutine runs every clock. On the falling edge it drives the inputs (config writes, the
ADC mux slot, the host's m_ready); in the ReadOnly phase of the same edge it samples outputs
and monitors. Values sampled there are what the next rising edge acts on, so a byte counts as
taken when m_valid and m_ready are both 1 at that sample.

  ADC mux    one slot per clock, n_slots per frame, s_frame on slot 0, never waits. Runs from
             reset with random filler; play() inserts data frames at the next frame boundary
             after the config queue is empty. Frame lengths can be faulted (frame_lens).
  config     writes through nlc_core's cfg port, one per clock (configure(), write()).
  host       m_ready per clock: turnaround (clocks per byte), random p_ready, stall windows.
  scoreboard splits bytes on m_last; checks header and seq; compares each packet with
             LossyCodec on the frames it covers (by seq, so expected losses are allowed);
             decodes; SNR and bits/sample.
  monitors   RTL only (needs u_enc.u_lossy): latency, bandwidth, FIFO occupancy, coverage.
  report     write_report() -> JSON in $NLC_RESULTS (default test/results/<suite>).

Times are clock indices (one per falling edge); us = clocks * clk_ns / 1000.
"""

from __future__ import annotations

import json
import os
import random
import sys
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path

import cocotb
import numpy as np
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge, ReadOnly

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "model"))
from nlc import stimgen  # noqa: E402
from nlc.data import load_challenge, synthetic  # noqa: E402
from nlc.lossy import LossyCodec, LossyConfig, default_tables, stream_coeffs  # noqa: E402
from nlc.packet import decode_header, decode_packet, encode_stream  # noqa: E402

CLK_NS = float(os.environ.get("CLK_NS", "200"))   # D2: clock = ADC slot rate, 5 MHz
N_SLOTS = 256
N_SEL_HW = 8          # nlc_core N_SEL: the RTL always flushes 8 rANS states (n_flush)
FLUSH_TAIL = 25       # bytes at the end of a packet counted as "flush" (8 x 3 + rounding)

REG_CTRL, REG_N_SEL, REG_SEL_SLOT = 0x00, 0x01, 0x10
MODE_LOSSY, CTRL_ENABLE = 1, 0x80


def codec() -> LossyCodec:
    return LossyCodec(LossyConfig(n_flush=N_SEL_HW), default_tables())


# ---------------------------------------------------------------------------
# data sources: full ADC frames (n_frames, n_slots)
# ---------------------------------------------------------------------------

_REAL: np.ndarray | None = None


def _real() -> np.ndarray | None:
    global _REAL
    raw = ROOT / "data" / "raw"
    if _REAL is None and raw.exists():
        _, _REAL = load_challenge(raw, N_SEL_HW, first_file=300)   # not in the ROM training set
    return _REAL


def frames(source: str, n_frames: int, slots: list[int], seed: int = 0, offset: int = 5000,
           n_slots: int = N_SLOTS) -> np.ndarray:
    """ADC frames for a data source. Unselected slots get random filler (seeded).

    real       challenge files 300+ (synthetic if data/raw is absent)
    synthetic  model/nlc/data.synthetic
    ramp, lfsr, constant   the on-chip generator's patterns (model/nlc/stimgen.py), all slots
    zeros, full            every selected sample 0 / 1023
    nyquist    full-scale square at the per-channel Nyquist rate (0, 1023, 0, ...)
    step       DC 200, then 800 from the middle of the run
    """
    rng = np.random.default_rng(seed)
    if source in stimgen.PATTERNS:
        out = stimgen.stream(source, n_frames)
        return out if n_slots == N_SLOTS else out[:, :n_slots]
    out = rng.integers(0, 1024, size=(n_frames, n_slots), dtype=np.int64)
    k = len(slots)
    if source == "real":
        r = _real()
        sel = (r[offset:offset + n_frames, :k] if r is not None
               else synthetic(k, n_frames, seed=seed))
    elif source == "synthetic":
        sel = synthetic(k, n_frames, seed=seed)
    elif source == "zeros":
        sel = np.zeros((n_frames, k), dtype=np.int64)
    elif source == "full":
        sel = np.full((n_frames, k), 1023, dtype=np.int64)
    elif source == "nyquist":
        sel = np.repeat((np.arange(n_frames) & 1)[:, None] * 1023, k, axis=1)
    elif source == "step":
        sel = np.where(np.arange(n_frames)[:, None] < n_frames // 2, 200, 800).repeat(k, 1)
    else:
        raise ValueError(f"unknown source {source}")
    out[:, slots] = sel
    return out


def dependency_horizon(cfg: LossyConfig) -> list[int]:
    """For sample n of a block: index of the last sample in the block whose push carries a
    symbol that depends on sample n (the transform's look-ahead, from the model)."""
    B = cfg.block_len
    base = stream_coeffs([0] * B, cfg.levels)
    where = {(c, p): i for i, pushed in enumerate(base) for c, p, _ in pushed}
    h = []
    for n in range(B):
        s = [0] * B
        s[n] = 1 << 12
        last = n
        for i, (pa, pb) in enumerate(zip(base, stream_coeffs(s, cfg.levels))):
            for (c, p, va), (_, _, vb) in zip(pa, pb):
                if va != vb:
                    last = max(last, i)
                    if c == 0 and (0, p + 1) in where:   # a3 is delta-coded: next symbol too
                        last = max(last, where[(0, p + 1)])
        h.append(last)
    return h


# ---------------------------------------------------------------------------
# host model
# ---------------------------------------------------------------------------

@dataclass
class Stall:
    """Host stops taking bytes for `clocks` clocks, starting at an absolute clock or when it is
    about to take byte `at_byte` of the `at_packet`-th packet it receives (0-based)."""
    clocks: int
    at_clock: int | None = None
    at_packet: int | None = None
    at_byte: int = 0


@dataclass
class Host:
    turnaround: int = 1          # clocks per byte: ready again `turnaround` clocks after a take
    p_ready: float = 1.0         # otherwise ready with this probability
    stalls: list[Stall] = field(default_factory=list)
    seed: int = 0

    def __post_init__(self) -> None:
        self.rng = random.Random(self.seed)
        self.next_ok = 0
        self.stall_until = -1
        self.stall_log: list[tuple[int, int, int]] = []   # (clock, packet, byte) of each start

    def ready(self, clock: int, pkt: int, byte: int) -> bool:
        for s in self.stalls:
            hit = (s.at_clock == clock) if s.at_clock is not None else (
                s.at_packet == pkt and s.at_byte == byte)
            if hit and clock > self.stall_until:
                self.stall_until = clock + s.clocks - 1
                self.stall_log.append((clock, pkt, byte))
                if s.at_clock is None:
                    s.at_packet = None            # one shot
        if clock <= self.stall_until or clock < self.next_ok:
            return False
        return self.p_ready >= 1.0 or self.rng.random() < self.p_ready

    def took(self, clock: int) -> None:
        self.next_ok = clock + self.turnaround


# ---------------------------------------------------------------------------
# scoreboard
# ---------------------------------------------------------------------------

class Segment:
    """Packets expected from one enable period: frames played from the first frame after
    enable. Packet k covers frames [k*fpp, (k+1)*fpp) of the selected slots."""

    def __init__(self, x_sel: np.ndarray, first_clock: int, n_slots: int, slots: list[int]):
        self.x = x_sel
        self.slots = slots
        self.first_clock = first_clock      # clock of slot 0 of frame 0
        self.n_slots = n_slots
        self.codec = codec()
        self.expected = encode_stream(x_sel, self.codec)
        self.next = 0
        self.got: dict[int, bytes] = {}
        self.got_clock: dict[int, int] = {}
        self.lost: list[int] = []


class Scoreboard:
    """allow_loss: seq gaps are recorded as lost packets, not errors (overflow tests).
    grace: mismatches in the first `grace` packets of a segment are notes, not errors.
    resync: on a mismatch, look for the packet at a frame offset of up to +-resync frames
    (frame-length faults shift the stream); a match there is a note."""

    def __init__(self, allow_loss: bool = False, grace: int = 0, resync: int = 0) -> None:
        self.allow_loss = allow_loss
        self.grace, self.resync = grace, resync
        self.notes: list[str] = []
        self.segments: list[Segment] = []
        self.errors: list[str] = []
        self.packets = 0
        self.extra = 0
        self.seq_log: list[int] = []

    @property
    def seg(self) -> Segment | None:
        return self.segments[-1] if self.segments else None

    def on_packet(self, pkt: bytes, clock: int) -> None:
        self.packets += 1
        seg = self.seg
        if seg is None:
            self.errors.append(f"packet of {len(pkt)} bytes at clock {clock} with no data played")
            return
        mode, seq = decode_header(pkt[0])
        self.seq_log.append(seq)
        k = seg.next
        if mode != MODE_LOSSY:
            self.errors.append(f"packet {k}: header mode {mode}, expected {MODE_LOSSY}")
        if seq != k & 63:
            if not self.allow_loss:
                self.errors.append(f"packet {k}: seq {seq}, expected {k & 63}")
            else:
                gap = (seq - k) & 63
                seg.lost += list(range(k, k + gap))
                k += gap
        seg.next = k + 1
        if k >= len(seg.expected):              # filler after the played data: not checked
            self.extra += 1
            return
        seg.got[k], seg.got_clock[k] = pkt, clock
        exp = seg.expected[k]
        if pkt != exp:
            i = next((i for i, (a, b) in enumerate(zip(pkt, exp)) if a != b), min(len(pkt), len(exp)))
            msg = f"packet {k}: first difference at byte {i} (got {len(pkt)} bytes, expected {len(exp)})"
            o = self._offset(seg, k, pkt)
            if o is not None:
                self.notes.append(f"packet {k}: matches the model at a frame offset of {o:+d}")
            elif k < self.grace:
                self.notes.append(msg + " (grace)")
            else:
                self.errors.append(msg)
            return
        try:
            _, y = decode_packet(pkt, seg.codec, len(seg.slots))
        except Exception as e:  # noqa: BLE001  decoder raises on any malformed packet
            self.errors.append(f"packet {k}: does not decode ({e})")
            return
        fpp = seg.codec.cfg.frames_per_packet
        ref = seg.x[k * fpp:(k + 1) * fpp]
        if y.shape != ref.shape:
            self.errors.append(f"packet {k}: decoded shape {y.shape}, expected {ref.shape}")

    def _offset(self, seg: Segment, k: int, pkt: bytes) -> int | None:
        fpp = seg.codec.cfg.frames_per_packet
        for o in sorted(range(-self.resync, self.resync + 1), key=abs):
            t0 = k * fpp + o
            if o == 0 or t0 < 0 or t0 + fpp > len(seg.x):
                continue
            p = encode_stream(seg.x[t0:t0 + fpp], seg.codec)[0]
            if bytes([p[0] & 0xC0 | k & 63]) + p[1:] == pkt:
                return o
        return None

    def quality(self) -> dict:
        err = sig = bits = n = 0.0
        for seg in self.segments:
            fpp = seg.codec.cfg.frames_per_packet
            for k, pkt in seg.got.items():
                if pkt != seg.expected[k]:
                    continue
                _, y = decode_packet(pkt, seg.codec, len(seg.slots))
                ref = seg.x[k * fpp:(k + 1) * fpp].astype(float)
                err += ((y - ref) ** 2).sum()
                sig += ((ref - ref.mean(axis=0)) ** 2).sum()
                bits += 8 * len(pkt)
                n += ref.size
        if not n:
            return {}
        return {"bits_per_sample": bits / n,
                "snr_db": float(10 * np.log10(sig / err)) if err else None}


# ---------------------------------------------------------------------------
# environment
# ---------------------------------------------------------------------------

def _int(sig, default: int = -1) -> int:
    v = sig.value
    return int(v) if v.is_resolvable else default


class NlcEnv:
    def __init__(self, dut, name: str, *, host: Host | None = None, n_slots: int = N_SLOTS,
                 clk_ns: float = CLK_NS, monitors: bool = True, allow_loss: bool = False,
                 grace: int = 0, resync: int = 0, seed: int = 0) -> None:
        self.dut, self.name = dut, name
        self.host = host or Host()
        self.n_slots, self.clk_ns = n_slots, clk_ns
        self.sb = Scoreboard(allow_loss, grace, resync)
        self._task = None
        self.rng = np.random.default_rng(seed)
        self.clock = 0
        self.cfg_q: deque[tuple[int, int]] = deque()
        self.cfg_idle = 0                       # clocks since the last config write
        self.slots: list[int] = []
        self.enabled = False
        # ADC
        self.frame: np.ndarray = self._filler()
        self.slot = 0
        self.frame_lens: deque[int] = deque()   # fault injection: next frames' lengths
        self.play_q: deque[np.ndarray] = deque()
        self.pending_seg: tuple | None = None
        self.adc_frame = 0                      # ADC frames started (all, incl. filler)
        self.data_frame = -1                    # index into the current segment, -1 = filler
        self.data_frame_clock: dict[int, int] = {}
        # host side
        self.rx = bytearray()
        self.rx_first_clock = 0
        self.stall_pos: list[tuple[int, int]] = []    # (packet, byte) where a stall started
        # monitors
        self.lossy = self.rans = None
        if monitors:
            try:
                self.lossy = dut.u_enc.u_lossy
                self.rans = self.lossy.u_rans
                _ = self.lossy.r_valid.value
            except AttributeError:
                self.lossy = self.rans = None
        self.mon = Mon()

    # -- setup ---------------------------------------------------------------
    async def start(self, reset_clocks: int = 5, clock: bool = True, before_reset=None) -> None:
        """Start the clock (once per cocotb test), reset the DUT, start the per-clock loop.
        before_reset(dut) runs while rst_n is low (e.g. to deposit power-up state)."""
        d = self.dut
        if clock:
            cocotb.start_soon(Clock(d.clk, self.clk_ns, unit="ns").start())
        for s in (d.s_valid, d.s_frame, d.s_data, d.cfg_we, d.cfg_addr, d.cfg_data, d.m_ready):
            s.value = 0
        d.rst_n.value = 0
        await ClockCycles(d.clk, 1)
        if before_reset is not None:
            before_reset(d)
        await ClockCycles(d.clk, reset_clocks)
        d.rst_n.value = 1
        self._task = cocotb.start_soon(self._loop())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def until(self, pred, max_clocks: int) -> bool:
        """Wait (checking every clock) until pred(env) is true."""
        for _ in range(max_clocks):
            if pred(self):
                return True
            await FallingEdge(self.dut.clk)
        return False

    async def reset(self, clocks: int = 3) -> None:
        """Pulse rst_n (synchronous) from the main loop's point of view."""
        await FallingEdge(self.dut.clk)
        self.dut.rst_n.value = 0
        await ClockCycles(self.dut.clk, clocks, rising=False)
        self.dut.rst_n.value = 1
        self.enabled = False
        self.cfg_q.clear()

    def write(self, addr: int, data: int) -> None:
        self.cfg_q.append((addr, data))

    def configure(self, slots: list[int], enable: bool = True) -> None:
        """Lossy mode, n_sel = len(slots). Written with enable = 0, then enable."""
        self.slots = list(slots)
        self.write(REG_CTRL, MODE_LOSSY)
        self.write(REG_N_SEL, len(slots))
        for i, s in enumerate(slots):
            self.write(REG_SEL_SLOT + i, s)
        if enable:
            self.write(REG_CTRL, MODE_LOSSY | CTRL_ENABLE)
        m = self.mon.cov
        m[f"n_sel_{len(slots)}"] += 1
        m["slot0_selected"] += 0 in slots
        m["slot255_selected"] += 255 in slots
        m["adjacent_selected"] += any(b == a + 1 for a, b in zip(slots, slots[1:]))

    def disable(self) -> None:
        self.write(REG_CTRL, MODE_LOSSY)

    def enable(self) -> None:
        self.write(REG_CTRL, MODE_LOSSY | CTRL_ENABLE)

    def play(self, rows: np.ndarray) -> None:
        """Queue ADC frames (n_frames, n_slots). They start at the first frame boundary after
        the config queue drains, which is frame 0 of a new scoreboard segment."""
        self.play_q.extend(rows)
        self.pending_seg = (rows[:, self.slots].copy(), self.slots)

    def play_queue_clear(self) -> None:
        """Drop frames not yet played (the ADC goes back to filler)."""
        self.play_q.clear()
        self.pending_seg = None

    def play_source(self, source: str, n_packets: int, seed: int = 0, offset: int = 5000) -> None:
        fpp = codec().cfg.frames_per_packet
        self.play(frames(source, n_packets * fpp, self.slots, seed, offset, self.n_slots))

    async def run(self, n_packets: int, timeout_packets: float = 3.0) -> bool:
        """Wait until the current segment has delivered n_packets (received or lost) or the
        ADC time for timeout_packets more packets has passed. False on timeout."""
        fpp = codec().cfg.frames_per_packet
        limit = self.clock + int((n_packets + timeout_packets) * fpp * self.n_slots)
        while self.clock < limit:
            seg = self.sb.seg
            if seg is not None and not self.play_q and seg.next >= n_packets:
                return True
            await ClockCycles(self.dut.clk, 256)
        self.sb.errors.append(f"timeout: {self.sb.seg.next if self.sb.seg else 0}/{n_packets} "
                              f"packets after {self.clock} clocks")
        return False

    async def idle(self, clocks: int) -> None:
        await ClockCycles(self.dut.clk, clocks)

    # -- per-clock loop ------------------------------------------------------
    def _filler(self) -> np.ndarray:
        return self.rng.integers(0, 1024, size=self.n_slots)

    def _adc(self) -> tuple[int, int]:
        d = self.dut
        if self.slot == 0:
            n = self.frame_lens.popleft() if self.frame_lens else self.n_slots
            # data starts at the first frame boundary after the last config write took effect
            if self.play_q and not self.cfg_q and self.cfg_idle >= 1:
                if self.pending_seg is not None:
                    x, slots = self.pending_seg
                    self.sb.segments.append(Segment(x, self.clock, self.n_slots, slots))
                    self.pending_seg = None
                    self.data_frame = -1
                    self.data_frame_clock = {}
                row = self.play_q.popleft()
                self.data_frame += 1
                self.data_frame_clock[self.data_frame] = self.clock
            else:
                row = self._filler()
            self.frame = row[:n] if n <= len(row) else np.concatenate([row, self._filler()])[:n]
            self.adc_frame += 1
        v = int(self.frame[self.slot])
        frame = int(self.slot == 0)
        self.slot = (self.slot + 1) % len(self.frame)
        return v, frame

    async def _loop(self) -> None:
        d = self.dut
        while True:
            await FallingEdge(d.clk)
            # config port
            if self.cfg_q:
                a, v = self.cfg_q.popleft()
                d.cfg_we.value, d.cfg_addr.value, d.cfg_data.value = 1, a, v
                if a == REG_CTRL:
                    self._on_ctrl(bool(v & CTRL_ENABLE))
                self.cfg_idle = 0
            else:
                d.cfg_we.value = 0
                self.cfg_idle += 1
            # ADC
            v, f = self._adc()
            d.s_valid.value, d.s_frame.value, d.s_data.value = 1, f, v
            # host
            pos = len(self.rx)
            ready = self.host.ready(self.clock, self.sb.packets, pos)
            d.m_ready.value = int(ready)

            await ReadOnly()
            if ready and _int(d.m_valid) == 1:
                b, last = _int(d.m_data, 0), _int(d.m_last, 0)
                if not self.rx:
                    self.rx_first_clock = self.clock
                self.rx.append(b)
                self.host.took(self.clock)
                if last:
                    self._on_packet()
            if self.lossy is not None:
                self._monitor()
            self.clock += 1

    def _on_ctrl(self, en: bool) -> None:
        if self.enabled and not en and self.lossy is not None:
            m = self.mon
            m.cov["enable_drop_midframe"] += self.slot != 0
            m.cov["enable_drop_midblock"] += _int(self.lossy.n, 0) != 0
            m.cov["enable_drop_midpacket"] += _int(self.lossy.j, 0) != 0
            m.cov["enable_drop_flush"] += _int(self.rans.phase, 0) != 0
        if en and not self.enabled:
            self.mon.new_run()
        self.enabled = en

    def _on_packet(self) -> None:
        pkt, clk = bytes(self.rx), self.clock
        self.rx.clear()
        seg = self.sb.seg
        k = seg.next if seg else 0
        for c, p, b in self.host.stall_log:
            if p == self.sb.packets:
                where = "header" if b == 0 else "flush" if b >= len(pkt) - FLUSH_TAIL else "payload"
                self.mon.cov[f"host_stall_{where}"] += 1
        if self.sb.seq_log and self.sb.seq_log[-1] == 63 and pkt and pkt[0] & 63 == 0:
            self.mon.cov["seq_wrap"] += 1
        self.sb.on_packet(pkt, clk)
        if seg is not None:
            self.mon.delivery(seg, seg.next - 1, clk)

    def _monitor(self) -> None:
        m, ly, rn, d = self.mon, self.lossy, self.rans, self.dut
        smp_v = _int(ly.smp_valid, 0)
        if smp_v:
            ch = _int(ly.smp_ch, 0)
            pushes = _int(ly.pushes, 0)
            for _ in range(pushes):
                m.fifo[ch].append((m.frame, self.clock))
            m.cov[f"occ_{_int(ly.occ, 0)}"] += 1
            if _int(ly.smp_last, 0):
                m.frame += 1
        r_valid = _int(ly.r_valid, 0)
        if r_valid:
            if _int(ly.r_ready, 0):
                self._fire(m, ly)
            else:
                m.cov["rans_hazard_stall"] += 1
        else:
            m.prev_fire = None
        if _int(rn.a_v, 0) and _int(rn.adv, 0):
            m.cov[f"coder_word_bytes_{_int(rn.o_n, 0)}"] += 1
        if smp_v and _int(rn.phase, 0) != 0:
            m.cov["flush_overlaps_samples"] += 1
        if _int(ly.overflow, 0):
            m.ly_overflow = True
        m.pending_max = max(m.pending_max, _int(ly.pending, 0))
        if _int(d.wr_en, 0):
            m.bytes_frame[self.adc_frame] += 1
            m.pkt_bytes += 1
            if m.flush_start is not None:
                m.flush_bytes += 1
            if _int(d.u_fifo.wr_last, 0):
                m.packet_lens.append(m.pkt_bytes)
                m.pkt_bytes = 0
                if m.flush_start is not None:
                    m.flush.append((m.flush_bytes, self.clock - m.flush_start))
                    m.flush_start = None
        if _int(d.fifo_full, 0):
            m.cov["out_fifo_full"] += 1
        m.out_peak = max(m.out_peak, _int(d.fifo_count, 0))
        if _int(d.overflow, 0):
            m.core_overflow = True

    def _fire(self, m: Mon, ly) -> None:
        ch = _int(ly.rr, 0)
        ctx, sym, esc = _int(ly.hctx, 0), _int(ly.hsym, 0), _int(ly.is_esc, 0)
        last = _int(ly.r_last, 0)
        m.sym[ctx][sym] += 1
        if esc:
            m.cov[f"esc_ctx{ctx}"] += 1
            if m.prev_esc:
                m.cov["esc_consecutive"] += 1
            if last:
                m.cov["esc_last_symbol"] += 1
        m.prev_esc = bool(esc)
        if m.prev_fire == (self.clock - 1, ch):
            m.cov["back_to_back_same_channel"] += 1
        m.prev_fire = (self.clock, ch)
        if m.fifo[ch]:
            f, t = m.fifo[ch].popleft()
            m.queue_lat.append(self.clock - t)
            m.last_fire[(ch, f)] = self.clock
        else:
            m.errors.append(f"clock {self.clock}: coder took a symbol of channel {ch} "
                            f"that the monitor never saw pushed")
        if last:
            m.flush_start, m.flush_bytes = self.clock, 0

    # -- results ---------------------------------------------------------------
    @property
    def errors(self) -> list[str]:
        return self.sb.errors + self.mon.errors

    def us(self, clocks: float) -> float:
        return clocks * self.clk_ns / 1000

    def results(self) -> dict:
        m = self.mon
        r: dict = {"test": self.name, "clk_ns": self.clk_ns, "n_slots": self.n_slots,
                   "slots": self.slots, "clocks": self.clock,
                   "packets_received": self.sb.packets,
                   "packets_lost": sum(len(s.lost) for s in self.sb.segments),
                   "errors": self.errors[:50], "n_errors": len(self.errors),
                   "notes": (self.sb.notes + self.mon.history)[:50], "packets_extra": self.sb.extra,
                   **self.sb.quality()}
        if self.lossy is None:
            return r
        cfg = codec().cfg
        hz = dependency_horizon(cfg)
        B = cfg.block_len
        proc = []
        if self.sb.segments:                    # monitors keep the last segment only
            for f, c0 in self.data_frame_clock.items():
                blk = f - f % B
                for ch, s in enumerate(self.sb.seg.slots):
                    t = m.last_fire.get((ch, blk + hz[f % B]))
                    if t is not None:
                        proc.append(t - (c0 + s))
        q = np.array(m.queue_lat) if m.queue_lat else np.zeros(1)
        p = np.array(proc) if proc else np.zeros(1)
        dl = np.array(m.deliv) if m.deliv else np.zeros(1)
        bpf = list(m.bytes_frame.values()) or [0]
        r["latency"] = {
            "queue_us": {"median": self.us(np.median(q)), "max": self.us(q.max())},
            "processing_us": {"median": self.us(np.median(p)), "max": self.us(p.max()),
                              "samples": len(proc)},
            "delivery_ms": {"median": self.us(np.median(dl)) / 1000,
                            "max": self.us(dl.max()) / 1000, "packets": len(m.deliv)},
            "horizon_frames_max": max(h - i for i, h in enumerate(hz)),
        }
        r["bandwidth"] = {
            "bytes_per_frame_max": max(bpf),
            "bytes_per_frame_hist": dict(sorted(Counter(bpf).items())),
            "packet_bytes": m.packet_lens,
            "out_fifo_peak": m.out_peak,
            "flush_bytes_max": max((b for b, _ in m.flush), default=0),
            "flush_clocks_max": max((c for _, c in m.flush), default=0),
            "pending_max": m.pending_max,
        }
        r["overflow"] = {"core_pin": m.core_overflow, "lossy_flag": m.ly_overflow}
        cov = dict(m.cov)
        cov["symbols_seen"] = {c: sorted(k for k, v in m.sym[c].items() if v) for c in range(4)}
        r["coverage"] = cov
        return r

    def write_report(self, extra: dict | None = None) -> dict:
        r = self.results()
        if extra:
            r.update(extra)
        out = Path(os.environ.get("NLC_RESULTS", ROOT / "test" / "results" / "core"))
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{self.name}.json").write_text(json.dumps(r, indent=1, default=_json))
        return r


def _json(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    return str(o)


class Mon:
    """Monitor state (RTL only)."""

    def __init__(self) -> None:
        self.cov: Counter = Counter()
        self.sym = [Counter() for _ in range(4)]
        self.errors: list[str] = []
        self.queue_lat: list[int] = []
        self.deliv: list[int] = []
        self.bytes_frame: Counter = Counter()
        self.packet_lens: list[int] = []
        self.flush: list[tuple[int, int]] = []
        self.out_peak = 0
        self.pending_max = 0
        self.core_overflow = self.ly_overflow = False
        self.history: list[str] = []
        self.new_run()

    def new_run(self) -> None:
        # anomalies of an earlier enable period (e.g. an illegal config, T-ROB-3) are history
        self.history += [f"earlier run: {e}" for e in self.errors[:20]]
        self.errors = []
        self.frame = 0
        self.fifo: dict[int, deque] = {c: deque() for c in range(N_SEL_HW)}
        self.last_fire: dict[tuple[int, int], int] = {}
        self.prev_esc = False
        self.prev_fire = None
        self.pkt_bytes = 0
        self.flush_start = None
        self.flush_bytes = 0

    def delivery(self, seg: Segment, k: int, clock: int) -> None:
        """Latency of packet k's first sample: slot 0 of its first frame -> last byte taken."""
        fpp = seg.codec.cfg.frames_per_packet
        t0 = seg.first_clock + k * fpp * seg.n_slots + seg.slots[0]
        self.deliv.append(clock - t0)
