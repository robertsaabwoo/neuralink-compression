"""Output-diff diagnosis: localise a hardware fault from the chip's output bytes.

Inputs: the captured byte stream (per byte: data, m_last; plus abort tokens), the
configuration (selected slots, n_sel = their count) and the known stimulus (ADC frames).
Output: a report from stream level down to the register row / shared net most likely at
fault, with the RTL instance names (rtl_map.py, generated from src/).

  python scripts/bringup/diagnose.py --capture cap.txt --slots 3,40,41,90,128,200,254,255 \
         --stim frames.npy [--frame-offset N | --align 300] [--json out.json]
  python scripts/bringup/diagnose.py --rtl-map

Capture format (text): whitespace-separated tokens, '#' comments.
  XX      a data byte (hex)          XXL or 'XX L'   the byte had m_last = 1
  A       an abort token (m_last = 1 with m_valid = 0 on the TT pins, m_abort on nlc_core)
Stimulus: .npy (T, 256) full ADC frames (slot values, frame 0 = the first frame after
enable), or (T, n_sel) the selected channels only (then slot-selector faults can only be
tested among the selected slots); or --source real|ramp|lfsr|constant (selected channels
only for real data, offset --offset).

How it localises (see README.md): packets are independent and rANS decoding is a bijection,
so a capture from a chip whose *coder* works decodes cleanly to whatever symbols the chip
computed, even if they are wrong. The decoded symbols are compared with the golden model's;
each differing symbol has a channel, context (d1/d2/d3/a3), block and frame. A bit-exact
model of the datapath (datapath.py) then tests fault hypotheses against them: every
per-channel register row stuck (all power-up values), stuck bits in rows and in shared
nets, wrong slot per channel. If the capture does not decode, the fault is in the coder or
the byte stream: re-encoding the expected symbols with a flipped state bit, byte
drops/corruptions and flush-only differences are tried, and the clean prefix / suffix bound
the location.
"""

from __future__ import annotations

import argparse
import json
import sys
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "model"))
sys.path.insert(0, str(HERE))

import datapath  # noqa: E402
import rtl_map  # noqa: E402
from nlc.adc import selected_stream  # noqa: E402
from nlc.lossless import unzigzag, zigzag  # noqa: E402
from nlc.lossy import LossyCodec, LossyConfig, coding_order, default_tables  # noqa: E402
from nlc.packet import encode_stream  # noqa: E402

N_FLUSH = 8                         # the RTL always flushes 8 rANS states (N_SEL)
CTX_NAME = {0: "a3", 1: "d1", 2: "d2", 3: "d3"}
CFG = LossyConfig(n_flush=N_FLUSH)
FPP = CFG.frames_per_packet
SB = CFG.state_bytes
FLUSH_LEN = SB * N_FLUSH


# ---------------------------------------------------------------------------
# capture I/O
# ---------------------------------------------------------------------------

def read_capture(path) -> list[tuple]:
    """-> tokens: ('B', byte, last) | ('A',)"""
    toks = []
    for line in Path(path).read_text().splitlines():
        line = line.split("#", 1)[0].split()
        i = 0
        while i < len(line):
            t = line[i].upper()
            if t in ("A", "ABORT"):
                toks.append(("A",))
            else:
                last = t.endswith("L")
                v = int(t.rstrip("L"), 16)
                if not last and i + 1 < len(line) and line[i + 1].upper() == "L":
                    last, i = True, i + 1
                toks.append(("B", v, last))
            i += 1
    return toks


def write_capture(path, toks: list[tuple], header: str = "") -> None:
    out = [f"# {h}" for h in header.splitlines()] if header else []
    for t in toks:
        out.append("A" if t[0] == "A" else f"{t[1]:02x}" + (" L" if t[2] else ""))
    Path(path).write_text("\n".join(out) + "\n")


def split_packets(toks):
    """-> list of (bytes, end) with end in 'last' | 'abort' | 'eof'."""
    out, cur = [], bytearray()
    for t in toks:
        if t[0] == "A":
            out.append((bytes(cur), "abort"))
            cur = bytearray()
        else:
            cur.append(t[1])
            if t[2]:
                out.append((bytes(cur), "last"))
                cur = bytearray()
    if cur:
        out.append((bytes(cur), "eof"))
    return out


# ---------------------------------------------------------------------------
# golden model, instrumented
# ---------------------------------------------------------------------------

def _slot_info():
    """Per production index j of a packet: (block, ctx, band index, frame in packet)."""
    codec = LossyCodec(CFG, default_tables())
    lay = codec._layout                                   # (block, ctx, p)
    ps = datapath.push_slots()
    return [(b, k, p, b * datapath.BLOCK + ps[j % len(ps)][0])
            for j, (b, k, p) in enumerate(lay)]


SLOTS_INFO = _slot_info()
TABLES = default_tables()
M = TABLES[0][-1]
L = M << CFG.lsh


def to_sym(v: int) -> tuple[int, bytes | None]:
    if abs(v) <= CFG.s_max:
        return v + CFG.s_max, None
    return CFG.esc, zigzag(v).to_bytes(CFG.esc_bytes, "little")


@dataclass
class Stream:
    """A packet's symbol stream in coding order."""
    chans: list[int]
    ctxs: list[int]
    js: list[int]
    syms: list[int]
    raws: list

    @classmethod
    def from_values(cls, vals: np.ndarray, n_ch: int) -> "Stream":
        order = coding_order(CFG, n_ch)
        s = cls([], [], [], [], [])
        for c, j in order:
            sym, raw = to_sym(int(vals[c, j]))
            s.chans.append(c)
            s.ctxs.append(SLOTS_INFO[j][1])
            s.js.append(j)
            s.syms.append(sym)
            s.raws.append(raw)
        return s


def encode_track(st: Stream):
    """encode_core with bookkeeping: (payload bytes, start offset of each symbol's bytes,
    channel states before each symbol)."""
    states = [L] * N_FLUSH
    out = bytearray()
    starts, snaps = [], []
    for ch, k, s, raw in zip(st.chans, st.ctxs, st.syms, st.raws):
        starts.append(len(out))
        snaps.append(tuple(states))
        if raw is not None:
            out += raw
        cdf = TABLES[k]
        f, c = cdf[s + 1] - cdf[s], cdf[s]
        x = states[ch]
        while x >= f << (CFG.lsh + 8):
            out.append(x & 0xFF)
            x >>= 8
        states[ch] = (x // f) * M + (x % f) + c
    starts.append(len(out))
    snaps.append(tuple(states))
    for ch in range(N_FLUSH):
        out += states[ch].to_bytes(SB, "little")
    return bytes(out), starts, snaps


def encode_check(st: Stream, payload: bytes, i0: int, states0, pos0: int, flip_ch: int,
                 flip_bit: int) -> bool:
    """Does the stream re-encoded from symbol i0, with bit flip_bit of channel flip_ch's state
    flipped just before it, reproduce payload[pos0:] exactly? Early exit."""
    states = list(states0)
    states[flip_ch] ^= 1 << flip_bit
    if not L <= states[flip_ch] < (L << 8) and flip_ch == st.chans[i0]:
        pass                                    # out-of-range states still encode (chip does)
    pos, n = pos0, len(payload)
    for i in range(i0, len(st.chans)):
        ch, k, s, raw = st.chans[i], st.ctxs[i], st.syms[i], st.raws[i]
        if raw is not None:
            if payload[pos:pos + len(raw)] != raw:
                return False
            pos += len(raw)
        cdf = TABLES[k]
        f, c = cdf[s + 1] - cdf[s], cdf[s]
        x = states[ch]
        while x >= f << (CFG.lsh + 8):
            if pos >= n or payload[pos] != x & 0xFF:
                return False
            pos += 1
            x >>= 8
        states[ch] = (x // f) * M + (x % f) + c
    tail = b"".join((x & ((1 << 8 * SB) - 1)).to_bytes(SB, "little") for x in states)
    return payload[pos:] == tail


X_MASK = (1 << (CFG.prob_bits + CFG.lsh + 8)) - 1      # 22-bit state register


def encode_hooked(st: Stream, payload: bytes, i0: int, states0, pos0: int, fc=None, wb=None,
                  row=None) -> bool:
    """Re-encode from symbol i0 with a coder fault and compare to payload[pos0:] (early exit).
    fc(k, s, f, c) -> (f, c): ROM output; wb(x) -> x: write-back value (all channels);
    row = (ch, and, or): a stuck bit in channel ch's state row (read after its first write)."""
    states = list(states0)
    written = [bool(i0)] * N_FLUSH                       # from a snapshot: conservative
    pos, n = pos0, len(payload)
    for i in range(i0, len(st.chans)):
        ch, k, s, raw = st.chans[i], st.ctxs[i], st.syms[i], st.raws[i]
        if raw is not None:
            if payload[pos:pos + len(raw)] != raw:
                return False
            pos += len(raw)
        cdf = TABLES[k]
        f, c = cdf[s + 1] - cdf[s], cdf[s]
        if fc is not None:
            f, c = fc(k, s, f, c)
            if f == 0:
                return False
        x = states[ch]
        if row is not None and row[0] == ch and written[ch]:
            x = (x & row[1]) | row[2]
        while x >= f << (CFG.lsh + 8):
            if pos >= n or payload[pos] != x & 0xFF:
                return False
            pos += 1
            x >>= 8
        x = ((x // f) * M + (x % f) + c) & X_MASK
        if wb is not None:
            x = wb(x)
        states[ch] = x
        written[ch] = True
    tail = bytearray()
    for ch, x in enumerate(states):
        if row is not None and row[0] == ch and written[ch]:
            x = (x & row[1]) | row[2]
        tail += (x & X_MASK).to_bytes(SB, "little")
    return payload[pos:] == bytes(tail)


def decode_tolerant(payload: bytes, chans: list[int], ctxs: list[int]):
    """decode_core_raw that never raises: (symbols, raws, clean). clean = the stream was
    consumed exactly and every state returned to L (a valid rANS stream)."""
    pos = len(payload) - FLUSH_LEN
    clean = pos >= 0
    pos = max(pos, 0)
    tail = payload[-FLUSH_LEN:].rjust(FLUSH_LEN, b"\0")
    states = [int.from_bytes(tail[SB * c:SB * c + SB], "little") for c in range(N_FLUSH)]
    n = len(chans)
    out, raws = [0] * n, [None] * n
    for i in reversed(range(n)):
        ch, cdf = chans[i], TABLES[ctxs[i]]
        x = states[ch]
        slot = x % M
        s = bisect_right(cdf, slot) - 1
        x = (cdf[s + 1] - cdf[s]) * (x // M) + slot - cdf[s]
        guard = 0
        while x < L and guard < 4:
            guard += 1
            pos -= 1
            if pos < 0:
                clean, pos = False, 0
                x = x << 8
            else:
                x = (x << 8) | payload[pos]
        if x < L:
            clean = False
        if s == CFG.esc:
            pos -= CFG.esc_bytes
            if pos < 0:
                clean, pos = False, 0
            raws[i] = bytes(payload[pos:pos + CFG.esc_bytes]).ljust(CFG.esc_bytes, b"\0")
        states[ch] = x
        out[i] = s
    if pos != 0 or any(x != L for x in states):
        clean = False
    return out, raws, clean


def sym_value(s: int, raw) -> int:
    if s == CFG.esc:
        v = unzigzag(int.from_bytes(raw, "little"))
        return datapath._s(v, 13) if -4096 <= v < 4096 else v
    return s - CFG.s_max


# ---------------------------------------------------------------------------
# findings
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    level: str              # stream | packet | symbol
    kind: str               # machine-readable class
    text: str
    block: str = ""         # hardware block (report term)
    rtl: list = field(default_factory=list)
    confidence: str = ""    # exact | high | medium | low
    next_step: str = ""
    packet: int | None = None
    data: dict = field(default_factory=dict)


def _ctxs_desc(ks) -> str:
    return "/".join(CTX_NAME[k] for k in sorted(ks, key=lambda k: (k == 0, k)))


# which contexts a fault in each row first corrupts, and what follows downstream
ROW_SIGNATURE = {
    "e1": {1, 2, 3, 0}, "o1": {1, 2, 3, 0}, "dp1": {2, 3, 0},
    "e2": {2, 3, 0}, "o2": {2, 3, 0}, "dp2": {3, 0},
    "e3": {3, 0}, "o3": {3, 0}, "dp3": {0}, "qa": {0},
    "b0": {1}, "b1": {2}, "b2": {3}, "b3": {0},
}


class Diagnoser:
    def __init__(self, slots: list[int], frames: np.ndarray | None = None,
                 sel: np.ndarray | None = None, frame_offset: int = 0) -> None:
        self.slots = list(slots)
        self.n_ch = len(slots)
        self.frames = frames                     # (T, 256) or None
        if sel is None:
            sel = selected_stream(list(frames), self.slots)
        self.x = np.asarray(sel, dtype=np.int64)[frame_offset:]
        if frames is not None:
            self.frames = np.asarray(frames, dtype=np.int64)[frame_offset:]
        self.n_pk = self.x.shape[0] // FPP
        self._exp: dict = {}
        self.findings: list[Finding] = []
        self.log: list[str] = []

    # -- expected ------------------------------------------------------------
    def expected(self, k: int):
        """(header byte, payload, Stream, values (C, J), starts, snaps) of packet k."""
        if k not in self._exp:
            if k >= self.n_pk:
                return None
            xk = self.x[k * FPP:(k + 1) * FPP]
            vals = datapath.simulate(xk)[0]
            st = Stream.from_values(vals, self.n_ch)
            payload, starts, snaps = encode_track(st)
            ref = encode_stream(xk, LossyCodec(CFG, TABLES))[0]
            assert ref[1:] == payload, "instrumented encoder != golden model"
            self._exp[k] = (0x40 | (k & 63), payload, st, vals, starts, snaps)
        return self._exp[k]

    def add(self, f: Finding) -> Finding:
        self.findings.append(f)
        return f

    # -- top level -----------------------------------------------------------
    def run(self, toks) -> list[Finding]:
        pk = split_packets(toks)
        data = [t[1] for t in toks if t[0] == "B"]
        n_abort = sum(1 for t in toks if t[0] == "A")
        self.log.append(f"capture: {len(data)} bytes, {sum(1 for p in pk if p[1] == 'last')} "
                        f"packets, {n_abort} abort tokens; config n_sel={self.n_ch} slots="
                        f"{self.slots}; stimulus covers {self.n_pk} packets")
        if not data and not n_abort:
            self.add(Finding("stream", "no_output", "no output at all", "enable/config/clock",
                             [rtl_map.block("pins_out"), rtl_map.block("cfg_slots")], "high",
                             "check rst_n released, config written (enable bit, n_sel), clock "
                             "running, s_strobe/s_frame toggling; probe m_valid (uio[6])"))
            return self.findings
        if len(set(data)) == 1 and len(data) > 8:
            self.add(Finding("stream", "stuck_output", f"output stuck at 0x{data[0]:02x} "
                             f"({len(data)} bytes)", "output pins / FIFO",
                             [rtl_map.block("pins_out"), rtl_map.block("out_fifo")], "high",
                             "probe uo_out; check m_ack handshake"))
            return self.findings
        if not any(p[1] == "last" for p in pk) and len(data) > 64:
            self.add(Finding("stream", "no_last", f"{len(data)} bytes but m_last never set",
                             "output pins (uio[7]) / serialiser", [rtl_map.block("pins_out")],
                             "medium", "probe uio[7]; the packets cannot be framed"))
        self._pin_bits(pk)
        self._packets(pk)
        self._summarise()
        return self.findings

    # -- stream level --------------------------------------------------------
    def _pin_bits(self, pk) -> None:
        """A data bit constant over the whole capture although the expected stream toggles it."""
        got = b"".join(p for p, e in pk)
        exp = b"".join(bytes([self.expected(k)[0]]) + self.expected(k)[1]
                       for k in range(min(self.n_pk, max(1, len(pk)))) if self.expected(k))
        if len(got) < 64 or not exp:
            return
        for b in range(8):
            g = {(v >> b) & 1 for v in got}
            e = {(v >> b) & 1 for v in exp}
            if len(g) == 1 and len(e) == 2:
                v = g.pop()
                self.pin_stuck = (b, v)
                self.add(Finding("stream", "pin_stuck", f"data bit {b} is {v} in every captured "
                                 f"byte (expected to toggle): uo_out[{b}] stuck-at-{v}",
                                 f"output pin uo_out[{b}] / pad / FIFO bit {b}",
                                 [rtl_map.block("pins_out"), rtl_map.block("out_fifo")], "high",
                                 f"probe uo_out[{b}] on the board; check the pad and the "
                                 f"capture wiring", data={"bit": b, "value": v}))

    def _packets(self, pk) -> None:
        k_next, prev_end = 0, "last"
        self.pk_results = []
        for idx, (p, end) in enumerate(pk):
            if end == "abort":
                seq = p[0] & 63 if p else None
                self.add(Finding("stream", "abort", f"abort token after {len(p)} bytes of a "
                                 f"packet (header seq {seq}): the chip dropped the packet in "
                                 "flight (D6/D7: host stall, short frame or enable drop)",
                                 "protocol event (not a fault by itself)",
                                 [rtl_map.block("abort")], "high",
                                 "expect a seq gap next; if no stall/short frame was applied, "
                                 "check the host ack timing and s_frame spacing",
                                 packet=k_next))
                prev_end = "abort"
                continue
            if end == "eof":
                self.add(Finding("stream", "trailing", f"{len(p)} bytes after the last complete "
                                 "packet (no m_last): capture stopped mid-packet, or m_last lost",
                                 "capture / m_last", [rtl_map.block("pins_out")], "low",
                                 "capture longer; if this repeats, probe uio[7]"))
                continue
            k = self._packet(p, k_next, prev_end)
            if k is not None:
                k_next = k + 1
            prev_end = end

    def _choose_k(self, p: bytes, k_next: int):
        """Which stimulus packet is this? Candidates: the continuation and the seq's claim."""
        seq = p[0] & 63
        cands = [k_next]
        g = (seq - k_next) & 63
        if g:
            cands.append(k_next + g)
        best, best_score = None, -1.0
        for k in cands:
            e = self.expected(k)
            if e is None:
                continue
            pay = p[1:]
            n = next((i for i, (a, b) in enumerate(zip(pay, e[1])) if a != b), min(len(pay),
                                                                                     len(e[1])))
            score = n / max(1, len(e[1]))
            syms, raws, clean = decode_tolerant(pay, e[2].chans, e[2].ctxs)
            if clean:
                vals = [sym_value(s, r) for s, r in zip(syms, raws)]
                ev = [sym_value(s, r) for s, r in zip(e[2].syms, e[2].raws)]
                score = max(score, np.mean(np.array(vals) == np.array(ev)))
            if score > best_score + 1e-9 or (abs(score - best_score) < 1e-9 and k != k_next):
                best, best_score = k, score
        return best, best_score

    def _packet(self, p: bytes, k_next: int, prev_end: str):
        mode, seq = p[0] >> 6, p[0] & 63
        k, score = self._choose_k(p, k_next)
        if k is None:
            self.add(Finding("packet", "no_stimulus", f"packet with seq {seq}: beyond the "
                             f"stimulus ({self.n_pk} packets) - not checked", packet=k_next))
            return k_next
        e = self.expected(k)
        hdr, exp_pay = e[0], e[1]
        res = {"k": k, "len": len(p), "exp_len": 1 + len(exp_pay), "ok": False}
        self.pk_results.append(res)
        if k > k_next:
            lost = list(range(k_next, k))
            why = "after an abort token" if prev_end == "abort" else "with no abort token"
            self.add(Finding("stream", "seq_gap", f"seq gap {why}: packets {lost} lost "
                             f"(seq {k_next & 63}..{(k - 1) & 63})",
                             "protocol event" if prev_end == "abort" else "packet loss",
                             [rtl_map.block("abort")] if prev_end == "abort" else
                             [rtl_map.block("seq"), rtl_map.block("out_fifo")],
                             "high", "" if prev_end == "abort" else
                             "a whole packet vanished without a token: check the host for "
                             "dropped packets, the FIFO overflow flag, enable glitches",
                             packet=k, data={"lost": lost}))
        if p[0] != hdr:
            self._header(p[0], hdr, k, k_next)
        if p[1:] == exp_pay:
            res["ok"] = p[0] == hdr
            return k
        self._payload(p[1:], k)
        return k

    # -- header ----------------------------------------------------------------
    def _header(self, got: int, hdr: int, k: int, k_next: int) -> None:
        x = got ^ hdr
        bits = [b for b in range(8) if x >> b & 1]
        pin = getattr(self, "pin_stuck", None)
        if pin and bits == [pin[0]]:
            return                                  # explained by the stuck pin
        if x & 0xC0:
            txt = f"header mode bits {got >> 6} (expected 1)"
            blk = "serialiser header mux (mode constant) / uo_out[7:6]"
        else:
            txt = (f"header seq {got & 63}, expected {hdr & 63} (payload is packet {k}; "
                   f"seq differs in bit(s) {bits})")
            blk = "packet sequence counter / header mux"
        self.add(Finding("packet", "header", txt, blk,
                         [rtl_map.block("seq"), rtl_map.block("serialiser")], "high",
                         "compare several packets: a constant XOR bit = stuck seq bit; a "
                         "constant offset = seq register upset (it keeps counting from there)",
                         packet=k, data={"got": got, "exp": hdr, "xor": x}))

    # -- payload -----------------------------------------------------------------
    def _payload(self, pay: bytes, k: int) -> None:
        hdr, exp_pay, st, vals, starts, snaps = self.expected(k)
        n_exp = len(exp_pay)
        b_f = next((i for i, (a, b) in enumerate(zip(pay, exp_pay)) if a != b),
                   min(len(pay), n_exp))
        i_f = max(0, bisect_right(starts, b_f) - 1)
        loc = self._where(st, min(i_f, len(st.chans) - 1))
        in_flush = b_f >= n_exp - FLUSH_LEN
        self.add(Finding("packet", "first_diff", f"packet {k}: first differing byte {b_f + 1} "
                         f"of {n_exp + 1} (got {len(pay) + 1} bytes); "
                         + (f"inside the final-state flush" if in_flush else
                            f"emitted while coding symbol {i_f} ({loc})"),
                         packet=k, data={"byte": b_f + 1, "len": len(pay) + 1,
                                         "exp_len": n_exp + 1}))
        syms, raws, clean = decode_tolerant(pay, st.chans, st.ctxs)
        if clean:
            got = np.zeros_like(vals)
            for i, (s, r) in enumerate(zip(syms, raws)):
                got[st.chans[i], st.js[i]] = sym_value(s, r)
            if np.array_equal(got, vals):
                self.add(Finding("packet", "same_symbols", f"packet {k} decodes to the expected "
                                 "symbols but the bytes differ (non-canonical stream)",
                                 "rANS coder", [rtl_map.block("rans_coder")], "low", packet=k))
                return
            self._datapath(k, got, vals)
            return
        if self._byte_edits(pay, exp_pay, k):
            # a state flip whose effect never left the emitted bits looks the same
            if len(pay) == n_exp and self._state_flip(pay, k, st, starts, snaps, b_f, i_f):
                for f in self.findings[-2:]:
                    f.confidence = "ambiguous"
                self.findings[-1].text += (" - equally explained by the byte corruption above: "
                                           "a flip that only reached emitted bits")
            return
        if not in_flush or len(pay) != n_exp:
            if self._state_flip(pay, k, st, starts, snaps, b_f, i_f):
                return
        if len(pay) == n_exp and pay[:n_exp - FLUSH_LEN] == exp_pay[:n_exp - FLUSH_LEN]:
            self._flush(pay, exp_pay, k)
            return
        if self._coder_hyps(pay, k, st, starts, snaps, i_f):
            return
        self._coder_bounds(pay, k, st, syms, raws, b_f, i_f)

    def _where(self, st: Stream, i: int) -> str:
        c, j = st.chans[i], st.js[i]
        b, ctx, p, fr = SLOTS_INFO[j]
        return f"channel {c}, {CTX_NAME[ctx]}[{p}] of block {b}, frame {fr}"

    # -- symbol level: datapath --------------------------------------------------
    def _datapath(self, k: int, got: np.ndarray, exp: np.ndarray) -> None:
        diff = got != exp
        chans = [c for c in range(self.n_ch) if diff[c].any()]
        desc = []
        for c in chans:
            js = np.flatnonzero(diff[c])
            ctxs = {SLOTS_INFO[j][1] for j in js}
            b, ctx, p, fr = SLOTS_INFO[js[0]]
            desc.append(f"ch {c}: {len(js)} symbols in {_ctxs_desc(ctxs)}, first "
                        f"{CTX_NAME[ctx]}[{p}] block {b} frame {fr}")
        first_ctx = {c: {SLOTS_INFO[j][1] for j in np.flatnonzero(diff[c])} for c in chans}
        self.add(Finding("symbol", "symbols_differ", f"packet {k} decodes cleanly (coder and "
                         f"byte path OK) but {int(diff.sum())} symbols differ: "
                         + "; ".join(desc), packet=k,
                         data={"channels": chans, "n_diff": int(diff.sum()),
                               "ctx": {c: sorted(v) for c, v in first_ctx.items()}}))
        xk = self.x[k * FPP:(k + 1) * FPP]
        hyps = []
        # wrong slot / channel order: does channel c carry another slot's data? ("late":
        # the sample is written after the issuer read the row: coded one frame late)
        slot_expl = {}
        cols, cols_late = self._columns(k), self._columns(k, stale=True)
        for c in chans:
            m = [s for s, v in cols.items() if np.array_equal(v, got[c])]
            m += [f"{s} (late)" for s, v in cols_late.items()
                  if np.all((v == got[c]) | (v == datapath.WILD))]
            if m:
                slot_expl[c] = m
        if slot_expl:
            parts = [f"channel {c} carries slot {'/'.join(map(str, s))} (configured "
                     f"{self.slots[c]})" for c, s in slot_expl.items()]
            sl = [int(str(v[0]).split()[0]) for v in slot_expl.values()]
            perm = all(len(v) == 1 for v in slot_expl.values()) and all(
                s in self.slots for s in sl) and len(chans) > 1
            late = any("late" in str(v) for vs in slot_expl.values() for v in vs)
            full = set(slot_expl) == set(chans)
            hyps.append(Finding(
                "symbol", "slot", "; ".join(parts) + (
                    " - a permutation of the selected slots: channel index/order (smp_ch)"
                    if perm else "") + (
                    "; 'late' = that channel's row is written after the issuer read it, so "
                    "it codes the previous frame's burst: the sample got the wrong channel "
                    "index" if late else "") + (
                    "" if full else f"; channels {sorted(set(chans) - set(slot_expl))} "
                    "not explained"),
                "slot selector (smp_ch / slot compare) / config slot registers / pin capture",
                [rtl_map.block("slot_sel"), rtl_map.block("slot_sel_smp"),
                 rtl_map.block("cfg_slots")], "exact" if full else "medium",
                "read back the slot config; drive a ramp (value = f + 4 s identifies the slot "
                "of every sample) and re-run", packet=k,
                data={"channels": {c: s for c, s in slot_expl.items()}}))
            if not full:
                hyps = []
                self.add(Finding("symbol", "slot_partial", "; ".join(parts), "slot selector",
                                 [rtl_map.block("slot_sel")], "medium", packet=k))
        # per-channel rows
        if not hyps and len(chans) <= 2:
            for c in chans:
                hyps += self._row_hyps(k, c, xk[:, c:c + 1], got[c], first_ctx[c])
        # shared nets
        if not hyps:
            hyps += self._shared_hyps(k, xk, got, chans)
        if hyps:
            for h in hyps:
                self.add(h)
            return
        # nothing reproduced it exactly: signature only
        cands = []
        for c in chans:
            for r, sig in ROW_SIGNATURE.items():
                if min(first_ctx[c], key=lambda q: (q == 0, q)) == min(sig, key=lambda q: (q == 0, q)):
                    cands.append(rtl_map.row(r, c))
        all_ch = len(chans) == self.n_ch
        self.add(Finding("symbol", "datapath_unexplained",
                         "no single modelled fault reproduces the symbols exactly; "
                         + ("all channels affected: shared datapath (lifter / quantiser / "
                            "issuer) or slot selector" if all_ch else
                            f"channels {chans} only: per-channel state of those channels"),
                         "shared datapath" if all_ch else f"per-channel rows of {chans}",
                         cands[:8] if not all_ch else
                         [rtl_map.signal(s) for s in ("d1", "a1", "d2", "q1")], "low",
                         "capture more packets with a ramp stimulus; compare per-context error "
                         "patterns", packet=k))

    def _columns(self, k: int, stale: bool = False) -> dict:
        """Symbols every stimulus column would give (slot -> (J,) values)."""
        if self.frames is not None:
            fk = self.frames[k * FPP:(k + 1) * FPP]
            v = datapath.simulate(fk, stale=stale)[0]
            return {s: v[s] for s in range(fk.shape[1])}
        xk = self.x[k * FPP:(k + 1) * FPP]
        v = datapath.simulate(xk, stale=stale)[0]
        return {self.slots[c]: v[c] for c in range(self.n_ch)}

    def _row_hyps(self, k, c, xc, got_c, ctxs) -> list[Finding]:
        out = []
        n_sym = got_c.shape[0]
        for r, w in datapath.ROW_W.items():
            if not ctxs <= ROW_SIGNATURE[r]:
                continue
            H = 1 << w
            on = np.ones((H, 1), dtype=bool)
            val = np.arange(H, dtype=np.int64)[:, None]
            sim = datapath.simulate(xc, H=H, stuck={r: (on, val)})[:, 0, :]
            ok = np.flatnonzero((sim == got_c[None, :]).all(axis=1))
            if len(ok):
                vs = [int(datapath._s(v, w)) for v in ok]
                vtxt = (f"value {vs[0]}" if len(vs) == 1 else
                        f"any of {len(vs)} values ({vs[0]}..{vs[-1]})")
                out.append(Finding(
                    "symbol", "row_stuck", f"channel {c}, row {r} ({rtl_map.ROW_ROLE[r]}) never "
                    f"written - holds {vtxt}: reproduces all {n_sym} symbols of channel {c} "
                    "exactly", f"row {r} of channel {c} (g_ch[{c}].u_{r})",
                    [rtl_map.row(r, c)], "exact",
                    f"dead clock gate: check {rtl_map.row(r, c)['netlist_gate']} CLK/GATE "
                    "connectivity in the netlist (the OpenROAD disconnect seen this week); "
                    f"re-test with a stimulus that changes row {r} to separate it from "
                    "equivalent rows", packet=k,
                    data={"row": r, "channel": c, "values": vs[:16]}))
            # single stuck bit in the row (all bits, both polarities, one batch)
            bs = [(b, sa) for b in range(w) for sa in (0, 1)]
            a = np.array([[((1 << w) - 1) & ~(1 << b)] for b, _ in bs], dtype=np.int64)
            o = np.array([[sa << b] for b, sa in bs], dtype=np.int64)
            sim = datapath.simulate(xc, H=len(bs), rowbit={r: (a, o)})[:, 0, :]
            for h in np.flatnonzero((sim == got_c[None, :]).all(axis=1)):
                b, sa = bs[h]
                out.append(Finding(
                    "symbol", "row_bit", f"channel {c}, row {r} bit {b} stuck-at-{sa} "
                    f"reproduces channel {c} exactly", f"row {r} of channel {c}, bit {b}",
                    [rtl_map.row(r, c)], "exact",
                    "probe/scan that flop; check for a short in the layout", packet=k,
                    data={"row": r, "channel": c, "bit": b, "sa": sa}))
        return out

    def _shared_hyps(self, k, xk, got, chans) -> list[Finding]:
        hyp = []
        for s, w in datapath.SIG_W.items():
            for b in range(w):
                for sa in (0, 1):
                    hyp.append((s, b, sa))
        H = len(hyp)
        sig = {}
        for s, w in datapath.SIG_W.items():
            a = np.full((H, 1), (1 << w) - 1, dtype=np.int64)
            o = np.zeros((H, 1), dtype=np.int64)
            for h, (hs, b, sa) in enumerate(hyp):
                if hs == s:
                    a[h] &= ~(1 << b)
                    o[h] |= sa << b
            sig[s] = (a, o)
        sim = datapath.simulate(xk, H=H, sig=sig)
        match = (sim == got[None]).reshape(H, -1).all(axis=1)
        exact = [hyp[h] for h in np.flatnonzero(match)]
        if not exact:
            score = (sim == got[None]).reshape(H, -1).mean(axis=1)
            best = int(np.argmax(score))
            self.log.append(f"packet {k}: best shared stuck-at hypothesis {hyp[best]} explains "
                            f"{score[best]:.3f} of symbols (not exact)")
            return []
        names = ", ".join(f"{s}[{b}] stuck-at-{sa}" for s, b, sa in exact)
        sigs = sorted({s for s, _, _ in exact}, key=list(datapath.SIG_W).index)
        return [Finding("symbol", "shared_stuck", f"shared net stuck-at reproduces all symbols of "
                        f"all channels exactly: {names}" + (
                            " (equivalent: same output for this stimulus)" if len(exact) > 1
                            else ""),
                        " / ".join(rtl_map.SIG_ROLE[s] for s in sigs),
                        [rtl_map.signal(s) for s in sigs], "exact",
                        "probe the net / check the netlist for a tie or short; a stimulus that "
                        "exercises the other polarity separates equivalent candidates",
                        packet=k, data={"candidates": exact})]

    # -- coder / byte stream -------------------------------------------------------
    def _byte_edits(self, pay: bytes, exp: bytes, k: int) -> bool:
        n, m = len(pay), len(exp)
        txt = None
        if n == m:
            d = [i for i in range(n) if pay[i] != exp[i]]
            if 0 < len(d) <= 2:
                parts = [f"byte {i + 1}: 0x{exp[i]:02x} -> 0x{pay[i]:02x} (bits "
                         f"{[b for b in range(8) if (pay[i] ^ exp[i]) >> b & 1]})" for i in d]
                txt = f"corrupted byte(s) on an otherwise exact packet: " + "; ".join(parts)
                kind = "byte_corrupt"
        elif n == m - 1:
            i = next((i for i in range(n) if pay[i] != exp[i]), n)
            if pay[i:] == exp[i + 1:]:
                txt = f"byte {i + 1} (0x{exp[i]:02x}) missing, everything else exact"
                kind = "byte_drop"
        elif n == m + 1:
            i = next((i for i in range(m) if pay[i] != exp[i]), m)
            if pay[i + 1:] == exp[i:]:
                txt = f"extra byte 0x{pay[i]:02x} inserted before byte {i + 1}" + (
                    " (a duplicate: double-counted ack edge)" if i and pay[i] == pay[i - 1] else "")
                kind = "byte_insert"
        if txt is None:
            return False
        self.add(Finding("packet", kind, f"packet {k}: {txt}",
                         "byte path after the coder: output FIFO / pins / host capture",
                         [rtl_map.block("out_fifo"), rtl_map.block("pins_out")], "high",
                         "a lone byte error with exact symbols around it is downstream of the "
                         "coder: check m_ack timing (two clocks after an ack edge), signal "
                         "integrity of uo_out, the capture tool", packet=k))
        return True

    def _state_flip(self, pay, k, st, starts, snaps, b_f, i_f) -> bool:
        # channel whose bytes went wrong first: the symbol emitting byte b_f, or the first
        # symbol after it that emits (a symbol may emit nothing)
        n = len(st.chans)
        cands = []
        chs = []
        for i in range(min(i_f, n - 1), min(i_f + 64, n)):
            if starts[i + 1] > starts[i] and st.chans[i] not in chs:
                chs.append(st.chans[i])
            if len(chs) >= 2:
                break
        for ch in chs:
            for i in range(min(i_f, n - 1), -1, -1):
                if st.chans[i] != ch:
                    continue
                for b in range(CFG.prob_bits + CFG.lsh + 8):
                    if encode_check(st, pay, i, snaps[i], starts[i], ch, b):
                        cands.append((i, ch, b))
                if len(cands) > 4 or (starts[i] < b_f - 64):
                    break
            if cands:
                break
        if not cands:
            return False
        i, ch, b = cands[0]
        where = self._where(st, i)
        alts = "" if len(cands) == 1 else f" (also consistent: {[(c[0], c[2]) for c in cands[1:4]]})"
        self.add(Finding("symbol", "state_flip", f"packet {k}: rANS state of channel {ch} had "
                         f"bit {b} flipped just before symbol {i} ({where}); re-encoding the "
                         f"expected symbols with that flip reproduces the whole packet{alts}",
                         f"rANS state row of channel {ch}", [rtl_map.state_row(ch)], "exact",
                         "a single upset: re-run the same stimulus; if it repeats at the same "
                         "place, a marginal flop/hold path on the state row (check its timing "
                         "and clock-gate hold), otherwise SEU/noise", packet=k,
                         data={"channel": ch, "bit": b, "symbol": i}))
        return True

    def _coder_hyps(self, pay, k, st, starts, snaps, i_f) -> bool:
        """Coder faults beyond a single flip: a ROM entry bit, a write-back bit, a state-row
        bit stuck. Each re-encodes the expected symbols with the fault (early exit)."""
        n = len(st.chans)
        found = []
        pb, xw = CFG.prob_bits, CFG.prob_bits + CFG.lsh + 8
        # ROM: entries (ctx, sym) used shortly before the first wrong byte
        first_use = {}
        for i in range(n):
            first_use.setdefault((st.ctxs[i], st.syms[i]), i)
        lo = max(0, i_f - 96)
        entries = sorted({(st.ctxs[i], st.syms[i]) for i in range(lo, min(i_f + 2, n))})
        for kk, ss in entries:
            u = first_use[(kk, ss)]
            cdf = TABLES[kk]
            for fld, w in (("f", pb + 1), ("c", pb)):
                v = (cdf[ss + 1] - cdf[ss]) if fld == "f" else cdf[ss]
                for b in range(w):
                    sa = 1 - ((v >> b) & 1)               # the only polarity that changes it
                    def fc(k2, s2, f, c, kk=kk, ss=ss, fld=fld, b=b, sa=sa):
                        if (k2, s2) != (kk, ss):
                            return f, c
                        if fld == "f":
                            return (f & ~(1 << b)) | (sa << b), c
                        return f, (c & ~(1 << b)) | (sa << b)
                    if encode_hooked(st, pay, u, snaps[u], starts[u], fc=fc):
                        found.append(("rom", kk, ss, fld, b, sa))
        if found:
            parts = []
            for _, kk, ss, fld, b, sa in found[:4]:
                val = "ESC" if ss == CFG.esc else str(ss - CFG.s_max)
                parts.append(f"ROM entry {CTX_NAME[kk]} symbol {ss} (value {val}): "
                             f"{fld}[{b}] stuck-at-{sa}")
            self.add(Finding("symbol", "rom", f"packet {k}: " + "; ".join(parts)
                             + " - re-encoding with that table entry reproduces the packet",
                             "rANS probability ROM (f_s / c_s)", [rtl_map.block("rans_rom")],
                             "exact", "a ROM bit is a logic fault: check the netlist cone of "
                             "that in_fc bit for this address; every packet using the entry "
                             "breaks the same way", packet=k, data={"candidates": found}))
            return True
        # write-back bit stuck (shared: every symbol of every channel)
        for b in range(xw):
            for sa in (0, 1):
                a, o = X_MASK & ~(1 << b), sa << b
                if encode_hooked(st, pay, 0, snaps[0], 0, wb=lambda x, a=a, o=o: (x & a) | o):
                    found.append(("wb", b, sa))
        if found:
            txt = ", ".join(f"wb_x[{b}] stuck-at-{sa}" for _, b, sa in found)
            self.add(Finding("symbol", "coder_wb", f"packet {k}: coder write-back {txt} "
                             "reproduces the packet (all channels)",
                             "rANS divider / write-back (shared)", [rtl_map.block("rans_coder")],
                             "exact", "inspect the divider output / write-back adder bit",
                             packet=k, data={"candidates": found}))
            return True
        # one channel's state row with a stuck bit
        chs = sorted({st.chans[i] for i in range(max(0, i_f - 16), min(i_f + 16, n))})
        for ch in chs:
            for b in range(xw):
                for sa in (0, 1):
                    if encode_hooked(st, pay, 0, snaps[0], 0,
                                     row=(ch, X_MASK & ~(1 << b), sa << b)):
                        found.append(("row", ch, b, sa))
        if found:
            _, ch, b, sa = found[0]
            self.add(Finding("symbol", "state_row_bit", f"packet {k}: rANS state row of channel "
                             f"{ch}, bit {b} stuck-at-{sa} reproduces the packet",
                             f"rANS state row of channel {ch}", [rtl_map.state_row(ch)], "exact",
                             "probe that flop; a permanent stuck bit repeats in every packet",
                             packet=k, data={"candidates": found}))
            return True
        return False

    def _flush(self, pay, exp, k) -> None:
        n = len(exp)
        bad = []
        for c in range(N_FLUSH):
            g = int.from_bytes(pay[n - FLUSH_LEN + SB * c: n - FLUSH_LEN + SB * c + SB], "little")
            e = int.from_bytes(exp[n - FLUSH_LEN + SB * c: n - FLUSH_LEN + SB * c + SB], "little")
            if g != e:
                bad.append((c, g ^ e))
        parts = [f"channel {c} (xor 0x{x:06x}" + (f", bit {x.bit_length() - 1}" if x & (x - 1) == 0
                                                  else "") + ")" for c, x in bad]
        rtl = [rtl_map.state_row(c) for c, _ in bad] + [rtl_map.block("rans_flush")]
        self.add(Finding("symbol", "flush", f"packet {k}: payload exact, final-state flush wrong "
                         f"for " + ", ".join(parts),
                         "rANS state row(s) after the last symbol / flush mux" if len(bad) < 8
                         else "flush mux (f_ch, f_b) / output word", rtl, "high",
                         "one channel: that state row changed after its last write-back (upset "
                         "or late write); several: flush sequencing", packet=k,
                         data={"channels": [c for c, _ in bad]}))

    def _coder_bounds(self, pay, k, st, syms, raws, b_f, i_f) -> None:
        ev = [sym_value(s, r) for s, r in zip(st.syms, st.raws)]
        gv = [sym_value(s, r) for s, r in zip(syms, raws)]
        n = len(ev)
        i_b = next((i for i in reversed(range(n)) if gv[i] != ev[i]), -1)
        tail_ok = n - 1 - i_b
        txt = (f"packet {k} does not decode (not a valid rANS stream for the expected "
               f"schedule): the fault is in the coder or the byte stream. Forward: bytes exact up "
               f"to byte {b_f + 1} (symbol {i_f}, {self._where(st, min(i_f, n - 1))}). Backward "
               f"decode from the flush: the last {tail_ok} symbols decode exactly"
               + (f", first wrong (from the end) symbol {i_b}: {self._where(st, i_b)}"
                  if i_b >= 0 else ""))
        ch = st.chans[min(i_f, n - 1)]
        self.add(Finding("symbol", "coder", txt, "rANS coder / issuer / byte stream",
                         [rtl_map.block("rans_coder"), rtl_map.state_row(ch),
                          rtl_map.block("issuer"), rtl_map.block("serialiser")], "medium",
                         "no single state-bit flip, byte edit or flush error reproduces it; "
                         "capture the same stimulus again (transient vs permanent) and dump "
                         "nlc_rans state/handshake if a debug path exists", packet=k,
                         data={"b_f": b_f, "i_f": i_f, "i_b": i_b}))

    # -- aggregate -------------------------------------------------------------------
    def _summarise(self) -> None:
        res = getattr(self, "pk_results", [])
        n_ok = sum(r["ok"] for r in res)
        self.log.append(f"{n_ok}/{len(res)} complete packets exact")
        rows = [f for f in self.findings if f.kind == "row_stuck"]
        if rows:
            key = {(f.data["row"], f.data["channel"]) for f in rows}
            if len(key) == 1 and len(rows) > 1:
                vals = set.intersection(*[set(f.data["values"]) for f in rows])
                self.log.append(f"row-stuck hypothesis consistent over {len(rows)} packets; "
                                f"common values {sorted(vals)[:8]}")
        hdr = [f for f in self.findings if f.kind == "header" and not f.data["xor"] & 0xC0]
        if len(hdr) >= 2:
            xors = {f.data["xor"] for f in hdr}
            offs = {((f.data["got"] - f.data["exp"]) & 63) for f in hdr}
            first = min(f.packet for f in hdr)
            if len(xors) == 1 and bin(next(iter(xors))).count("1") == 1:
                b = next(iter(xors)).bit_length() - 1
                sa = (hdr[0].data["got"] >> b) & 1
                txt = (f"header seq bit {b} stuck-at-{sa}: same single-bit XOR on "
                       f"{len(hdr)} packets (payloads are the right packets)")
                kind = "seq_bit"
            elif len(offs) == 1:
                txt = (f"header seq offset constant (+{next(iter(offs))}) from packet {first} "
                       f"over {len(hdr)} packets, payloads continuous: seq register upset or "
                       "loaded wrong, then counting normally")
                kind = "seq_upset"
            else:
                txt = f"header seq wrong on {len(hdr)} packets, XOR patterns {sorted(xors)}"
                kind = None
            self.log.append(txt)
            if kind:
                self.add(Finding("packet", kind, txt, "packet sequence counter (serialiser)",
                                 [rtl_map.block("seq"), rtl_map.block("serialiser")], "high",
                                 "stuck bit: check the seq flop / header mux bit; upset: "
                                 "re-run, look for resets or glitches on the serialiser gate",
                                 packet=first, data={"xor": sorted(xors), "offset": sorted(offs)}))

    # -- report ------------------------------------------------------------------------
    def verdict(self) -> Finding | None:
        """The most specific finding (what the report leads with)."""
        rank = ["row_stuck", "row_bit", "shared_stuck", "slot", "state_flip", "rom", "coder_wb", "state_row_bit", "flush",
                "byte_corrupt", "byte_drop", "byte_insert", "pin_stuck", "seq_bit", "seq_upset", "header", "coder",
                "slot_partial", "datapath_unexplained", "seq_gap", "no_output", "stuck_output", "no_last",
                "trailing", "abort"]
        for r in rank:
            for f in self.findings:
                if f.kind == r and not (r == "state_flip" and f.confidence == "ambiguous"):
                    return f
        return None

    def report(self) -> str:
        out = ["NLC output-diff diagnosis", "=" * 25, *self.log, ""]
        v = self.verdict()
        if v is None:
            out.append("VERDICT: no fault found - every complete packet matches the golden model")
        else:
            out.append(f"VERDICT [{v.confidence}]: {v.text}")
            if v.confidence == "ambiguous":
                alt = [f for f in self.findings if f.confidence == "ambiguous" and f is not v]
                for f in alt:
                    out.append(f"  ALTERNATIVE: {f.text}")
            if v.block:
                out.append(f"  hardware: {v.block}")
            for r in v.rtl:
                out.append(f"  rtl: {r['rtl']}" + (f"  (netlist gate {r['netlist_gate']})"
                                                    if "netlist_gate" in r else "")
                           + f"  [{r['src']}]")
            if v.next_step:
                out.append(f"  next: {v.next_step}")
        for lvl in ("stream", "packet", "symbol"):
            fs = [f for f in self.findings if f.level == lvl]
            if not fs:
                continue
            out += ["", f"{lvl}-level:"]
            shown = 0
            for f in fs:
                if shown >= 12:
                    out.append(f"  ... {len(fs) - shown} more")
                    break
                shown += 1
                out.append(f"  - [{f.kind}{', ' + f.confidence if f.confidence else ''}] {f.text}")
                if f.block and f is not v:
                    out.append(f"      -> {f.block}")
        return "\n".join(out)

    def to_json(self) -> dict:
        v = self.verdict()
        return {"log": self.log, "verdict": v.__dict__ if v else None,
                "findings": [f.__dict__ for f in self.findings]}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def load_stimulus(args, slots):
    if args.stim:
        a = np.load(args.stim) if args.stim.endswith(".npy") else np.loadtxt(args.stim,
                                                                              delimiter=",")
        a = np.asarray(a, dtype=np.int64)
        if a.shape[1] == 256:
            return a, None
        if a.shape[1] == len(slots):
            return None, a
        raise SystemExit(f"stimulus has {a.shape[1]} columns: need 256 (frames) or n_sel")
    if args.source in ("ramp", "lfsr", "constant"):
        from nlc import stimgen
        return stimgen.stream(args.source, args.frames), None
    if args.source == "real":
        from nlc.data import load_challenge
        _, r = load_challenge(ROOT / "data" / "raw", 8, first_file=300)
        return None, r[args.offset:args.offset + args.frames, :len(slots)]
    raise SystemExit("need --stim or --source")


def align(slots, frames, sel, toks, max_off) -> int:
    """Frame offset at which the first complete packet matches best (symbol agreement)."""
    pk = [p for p, e in split_packets(toks) if e == "last"]
    if not pk:
        return 0
    best, best_s = 0, -1.0
    for off in range(max_off + 1):
        d = Diagnoser(slots, frames, sel, off)
        if d.n_pk == 0:
            break
        k, s = d._choose_k(pk[0], 0)
        if s > best_s:
            best, best_s = off, s
        if s == 1.0:
            break
    return best


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--capture")
    ap.add_argument("--slots", help="comma-separated selected slots (n_sel = count)")
    ap.add_argument("--config", help="JSON with 'slots'")
    ap.add_argument("--stim", help=".npy/.csv stimulus: (T,256) frames or (T,n_sel)")
    ap.add_argument("--source", choices=["real", "ramp", "lfsr", "constant"])
    ap.add_argument("--offset", type=int, default=5000, help="real data: first frame")
    ap.add_argument("--frames", type=int, default=4 * FPP)
    ap.add_argument("--frame-offset", type=int, default=0, help="stimulus frame of packet 0")
    ap.add_argument("--align", type=int, default=0, help="search the frame offset up to N")
    ap.add_argument("--json", help="write findings as JSON")
    ap.add_argument("--rtl-map", action="store_true", help="print the RTL reverse lookup")
    a = ap.parse_args(argv)
    if a.rtl_map:
        print(rtl_map.table())
        return 0
    if not a.capture:
        ap.error("--capture is required")
    slots = (json.loads(Path(a.config).read_text())["slots"] if a.config
             else [int(s) for s in a.slots.split(",")])
    frames, sel = load_stimulus(a, slots)
    toks = read_capture(a.capture)
    off = a.frame_offset
    if a.align:
        off = align(slots, frames, sel, toks, a.align)
    d = Diagnoser(slots, frames, sel, off)
    if a.align:
        d.log.append(f"aligned: packet 0 = stimulus frame {off}")
    d.run(toks)
    print(d.report())
    if a.json:
        Path(a.json).write_text(json.dumps(d.to_json(), indent=1, default=str))
    v = d.verdict()
    return 0 if v is None or v.kind in ("abort", "seq_gap") else 1


if __name__ == "__main__":
    sys.exit(main())
