"""Fast self-test of diagnose.py without the RTL: a software "chip" (datapath.py + the
instrumented encoder) produces captures with injected faults, the diagnoser must name them.
The RTL fault-injection run (test/bringup) is what confirms the tool; this keeps it honest
between RTL runs.   python scripts/bringup/selftest.py
"""

from __future__ import annotations

import sys
import time

import numpy as np

import diagnose as dg
from diagnose import FPP, Diagnoser, Stream, encode_track

SLOTS = [3, 40, 41, 90, 128, 200, 254, 255]


def stimulus(n_pk=2, seed=0):
    rng = np.random.default_rng(seed)
    frames = rng.integers(0, 1024, size=(n_pk * FPP, 256), dtype=np.int64)
    try:
        from nlc.data import load_challenge
        _, r = load_challenge(dg.ROOT / "data" / "raw", 8, first_file=300)
        frames[:, SLOTS] = r[5000:5000 + n_pk * FPP]
    except Exception:  # noqa: BLE001
        pass
    return frames


def chip(frames, slots=SLOTS, n_pk=2, stuck=None, rowbit=None, sig=None, ch=None,
         slot_map=None, seq_xor=0, flip=None):
    """Software chip -> capture tokens."""
    x = dg.selected_stream(list(frames), slots)
    if slot_map:
        x = x.copy()
        for c, s in slot_map.items():
            x[:, c] = frames[:, s]
    toks = []
    for k in range(n_pk):
        xk = x[k * FPP:(k + 1) * FPP]
        n = len(slots)
        if stuck or rowbit:
            # per-channel fault on channel ch only
            v = dg.datapath.simulate(xk)[0]
            vc = dg.datapath.simulate(xk[:, ch:ch + 1], stuck=stuck, rowbit=rowbit)[0, 0]
            v[ch] = vc
        else:
            v = dg.datapath.simulate(xk, sig=sig)[0]
        st = Stream.from_values(v, n)
        if flip and flip[0] == k:
            pay = _encode_flip(st, flip[1], flip[2])
        else:
            pay = encode_track(st)[0]
        pkt = bytes([0x40 | ((k & 63) ^ seq_xor)]) + pay
        toks += [("B", b, i == len(pkt) - 1) for i, b in enumerate(pkt)]
    return toks


def _encode_flip(st, i0, bit):
    payload, starts, snaps = encode_track(st)
    ch = st.chans[i0]
    states = list(snaps[i0])
    states[ch] ^= 1 << bit
    out = bytearray(payload[:starts[i0]])
    for i in range(i0, len(st.chans)):
        c, k, s, raw = st.chans[i], st.ctxs[i], st.syms[i], st.raws[i]
        if raw is not None:
            out += raw
        cdf = dg.TABLES[k]
        f, cc = cdf[s + 1] - cdf[s], cdf[s]
        x = states[c]
        while x >= f << (dg.CFG.lsh + 8):
            out.append(x & 0xFF)
            x >>= 8
        states[c] = (x // f) * dg.M + (x % f) + cc
    for c in range(dg.N_FLUSH):
        out += (states[c] & 0xFFFFFF).to_bytes(3, "little")
    return bytes(out)


def chip_coder(frames, slots=SLOTS, n_pk=1, fc=None, wb=None, row=None):
    """Software chip with a coder fault (same hook semantics as diagnose.encode_hooked)."""
    x = dg.selected_stream(list(frames), slots)
    toks = []
    for k in range(n_pk):
        st = Stream.from_values(dg.datapath.simulate(x[k * FPP:(k + 1) * FPP])[0], len(slots))
        out = bytearray()
        states, written = [dg.L] * dg.N_FLUSH, [False] * dg.N_FLUSH
        for ch, kk, s, raw in zip(st.chans, st.ctxs, st.syms, st.raws):
            if raw is not None:
                out += raw
            cdf = dg.TABLES[kk]
            f, c = cdf[s + 1] - cdf[s], cdf[s]
            if fc:
                f, c = fc(kk, s, f, c)
            xx = states[ch]
            if row and row[0] == ch and written[ch]:
                xx = (xx & row[1]) | row[2]
            while xx >= f << (dg.CFG.lsh + 8):
                out.append(xx & 0xFF)
                xx >>= 8
            xx = ((xx // f) * dg.M + (xx % f) + c) & dg.X_MASK
            states[ch] = wb(xx) if wb else xx
            written[ch] = True
        for ch, xx in enumerate(states):
            if row and row[0] == ch and written[ch]:
                xx = (xx & row[1]) | row[2]
            out += xx.to_bytes(3, "little")
        pkt = bytes([0x40 | k]) + bytes(out)
        toks += [("B", b, i == len(pkt) - 1) for i, b in enumerate(pkt)]
    return toks


def run(name, toks, frames, want):
    t = time.time()
    d = Diagnoser(SLOTS, frames)
    d.run(toks)
    v = d.verdict()
    ok = want(v)
    print(f"{'PASS' if ok else 'FAIL'} {name:28s} {time.time() - t:5.1f}s  "
          f"{(v.kind + ': ' + v.text[:150]) if v else 'no fault'}")
    return ok


def main() -> int:
    fr = stimulus()
    oks = []
    oks.append(run("clean", chip(fr), fr, lambda v: v is None))
    on = np.ones((1, 1), bool)
    oks.append(run("ch3 dp2 stuck", chip(fr, stuck={"dp2": (on, np.array([[777]]))}, ch=3), fr,
                   lambda v: v and v.kind == "row_stuck" and v.data["row"] == "dp2"
                   and v.data["channel"] == 3))
    oks.append(run("ch5 o1 stuck", chip(fr, stuck={"o1": (on, np.array([[100]]))}, ch=5), fr,
                   lambda v: v and v.kind == "row_stuck" and v.data["row"] == "o1"
                   and v.data["channel"] == 5))
    oks.append(run("ch1 b2 stuck", chip(fr, stuck={"b2": (on, np.array([[5]]))}, ch=1), fr,
                   lambda v: v and v.kind == "row_stuck" and v.data["row"] == "b2"))
    oks.append(run("ch6 e3 bit4 sa1", chip(fr, rowbit={"e3": (np.int64(0xFFF & ~16),
                                                                 np.int64(16))}, ch=6), fr,
                   lambda v: v and v.kind in ("row_bit", "row_stuck") and v.data["row"] == "e3"))
    sig = {"d2": (np.full((1, 1), 0xFFF, np.int64), np.full((1, 1), 8, np.int64))}
    oks.append(run("d2[3] sa1 shared", chip(fr, sig=sig), fr,
                   lambda v: v and v.kind == "shared_stuck" and ("d2", 3, 1) in v.data["candidates"]))
    oks.append(run("ch2 takes slot 42", chip(fr, slot_map={2: 42}), fr,
                   lambda v: v and v.kind == "slot" and v.data["channels"].get(2) == [42]))
    oks.append(run("ch1<->ch2 swapped", chip(fr, slot_map={1: 41, 2: 40}), fr,
                   lambda v: v and v.kind == "slot"))
    oks.append(run("state flip bit17", chip(fr, flip=(1, 900, 17)), fr,
                   lambda v: v and v.kind == "state_flip" and v.data["bit"] == 17))
    # a flip that only reaches bits emitted at once = a corrupted byte: must say ambiguous
    oks.append(run("state flip bit9 (low)", chip(fr, flip=(1, 900, 9)), fr,
                   lambda v: v and v.kind == "byte_corrupt" and v.confidence == "ambiguous"))
    toks = chip(fr)
    del toks[500]
    oks.append(run("dropped byte", toks, fr, lambda v: v and v.kind == "byte_drop"))
    toks = chip(fr)
    toks[700] = ("B", toks[700][1] ^ 0x10, toks[700][2])
    oks.append(run("corrupted byte", toks, fr, lambda v: v and v.kind == "byte_corrupt"))
    oks.append(run("seq bit stuck", chip(fr, seq_xor=2), fr,
                   lambda v: v and v.kind == "seq_bit"))
    def rom(k, s, f, c):
        return (f, c ^ 0x10) if (k, s) == (1, 31) else (f, c)
    oks.append(run("ROM d1 sym31 c[4] flip", chip_coder(fr, fc=rom), fr,
                   lambda v: v and v.kind == "rom" and (1, 31) in
                   [(c[1], c[2]) for c in v.data["candidates"]]))
    oks.append(run("write-back bit 13 sa1", chip_coder(fr, wb=lambda x: x | (1 << 13)), fr,
                   lambda v: v and v.kind == "coder_wb"
                   and ("wb", 13, 1) in v.data["candidates"]))
    oks.append(run("state row ch2 bit 20 sa0",
                   chip_coder(fr, row=(2, dg.X_MASK & ~(1 << 20), 0)), fr,
                   lambda v: v and v.kind == "state_row_bit"
                   and ("row", 2, 20, 0) in v.data["candidates"]))
    print(f"{sum(oks)}/{len(oks)} passed")
    return 0 if all(oks) else 1


if __name__ == "__main__":
    sys.exit(main())
