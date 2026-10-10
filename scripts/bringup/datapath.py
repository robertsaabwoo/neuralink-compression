"""Bit-exact model of nlc_lossy's per-channel datapath, with fault hooks (bring-up only).

Mirrors src/nlc_lossy.sv + src/nlc_lift53.sv signal by signal, at the RTL widths, so that a
hypothesised hardware fault can be simulated and compared against the symbols decoded from a
captured packet. Fault-free it equals model/nlc/lossy.py channel_symbols (checked by
self_check()).

Vectorised over a batch of hypotheses: every state row is an array of shape (H, C)
(H hypotheses x C channels), so e.g. all 4096 possible power-up values of a stuck 12-bit
row, or all stuck-at bits of all shared signals, run in one pass.

Faults (all optional):
  stuck  {row: (on (H,C) bool, value (H,C) int)}   row never written (clock gate dead): holds
                                                   value (its power-up content) where on
  rowbit {row: (and_mask, or_mask)}                stuck bit(s) in a row's flops (on read)
  sig    {signal: (and_mask, or_mask)}             stuck bit(s) on a shared combinational net

Rows (per channel, the nlc_greg rows of g_ch[c]): e1 o1 dp1 e2 o2 dp2 e3 o3 dp3 qa b0 b1 b2 b3.
Shared signals: x0 (centred sample = smp_data), d1 a1 (u_l1), d2 a2 (u_l2), d3 a3 (u_l3),
q1 q2 q3 qa3 da3 (quantiser / a3 delta), hv (issuer read mux = symbol value).
"""

from __future__ import annotations

import numpy as np

ROW_W = {"e1": 10, "o1": 10, "dp1": 11, "e2": 11, "o2": 11, "dp2": 12, "e3": 12, "o3": 12,
         "dp3": 13, "qa": 12, "b0": 8, "b1": 10, "b2": 11, "b3": 13}
SIG_W = {"x0": 10, "d1": 11, "a1": 11, "d2": 12, "a2": 12, "d3": 13, "a3": 13,
         "q1": 13, "q2": 13, "q3": 13, "qa3": 13, "da3": 13, "hv": 13}
BLOCK = 64
WILD = 1 << 20                      # stale mode: a row read before its first write (unknown)
CTX_OF_PUSH = (1, 2, 3, 0)          # burst slot k -> context (d1, d2, d3, a3)


def _u(v, w):
    return v & ((1 << w) - 1)


def _s(v, w):
    v = v & ((1 << w) - 1)
    return v - ((v >> (w - 1)) & 1) * (1 << w)


def _quant(c, sh):
    """quant() of nlc_lossy: sign(c) * (|c| >> sh), 13-bit two's complement."""
    cu = _u(c, 13)
    neg = (cu >> 12) & 1
    m = np.where(neg == 1, _u(-cu, 13), cu) >> sh
    return _s(np.where(neg == 1, _u(-m, 13), m), 13)


def _sched(idx: int, nw: int):
    """nlc_lift53's global schedule for input index idx at a level of 2^nw samples."""
    odd = idx & 1
    pv = (idx == (1 << nw) - 1) if odd else idx != 0
    p = (idx >> 1) if odd else (idx >> 1) - 1
    return odd, pv, p


def schedule():
    """Per frame of a block: (odd1, pv1, p1, odd2, pv2, p2, odd3, pv3, p3); the RTL's global
    level-enable schedule (same for every channel)."""
    out = []
    for n in range(BLOCK):
        o1, v1, p1 = _sched(n, 6)
        o2, v2, p2 = _sched(p1 & 31, 5) if v1 else (0, False, 0)
        o3, v3, p3 = _sched(p2 & 15, 4) if v2 else (0, False, 0)
        out.append((o1, v1, p1, o2, v1 and v2, p2, o3, v1 and v2 and v3, p3))
    return out


_SCHED = schedule()


def push_slots():
    """Production slots of one block: list of (frame in block, burst slot k)."""
    out = []
    for n, (_, v1, _, _, v2, _, _, v3, _) in enumerate(_SCHED):
        ks = [0] if v1 else []
        ks += [1] if v2 else []
        ks += [2, 3] if v3 else []
        out += [(n, k) for k in ks]
    return out


def simulate(x: np.ndarray, H: int = 1, stuck=None, rowbit=None, sig=None,
             stale: bool = False) -> np.ndarray:
    """x: (T, C) ADC codes, T a multiple of 64 (whole blocks, e.g. one packet).
    Returns (H, C, J) symbol values (signed, as issued) in production order.
    stale: the issuer reads the burst rows *before* the channel's sample writes them (a
    channel whose sample arrives after the issuer reached it: wrong channel index/order),
    so each frame codes the previous write's contents with this frame's burst size."""
    stuck, rowbit, sig = stuck or {}, rowbit or {}, sig or {}
    x = np.asarray(x, dtype=np.int64)
    T, C = x.shape
    shape = (H, C)
    rows = {r: np.zeros(shape, dtype=np.int64) for r in ROW_W}
    for r, (on, val) in stuck.items():
        rows[r] = np.where(on, _u(np.broadcast_to(val, shape), ROW_W[r]), 0).astype(np.int64)

    def rd(r):                                  # row read: sign-extended, with stuck bits
        v = rows[r]
        if r in rowbit:
            a, o = rowbit[r]
            v = (v & a) | o
        return _s(v, ROW_W[r])

    def wr(r, v):
        v = _u(np.broadcast_to(v, shape), ROW_W[r])
        if r in stuck:
            on = stuck[r][0]
            v = np.where(on, rows[r], v)
        rows[r] = v

    def f(name, v):                             # shared signal with stuck bits
        if name in sig:
            w = SIG_W[name]
            a, o = sig[name]
            v = _s((_u(v, w) & a) | o, w)
        return v

    def lift(xin, er, orow, dpr, w, odd, p, dname, aname):
        e, o, dp = rd(er), rd(orow), rd(dpr)
        e_nx = e if odd else xin
        o_cur = xin if odd else o
        s2 = e + e_nx
        d = f(dname, _s(o_cur - (s2 >> 1), w + 1))
        d_prev = d if p == 0 else dp
        a = f(aname, _s(e + ((d_prev + d + 2) >> 2), w + 1))
        return a, d

    out = []
    written: set = set()
    for t in range(T):
        o1, v1, p1, o2, v2, p2, o3, v3, p3 = _SCHED[t % BLOCK]
        x0 = f("x0", np.broadcast_to(x[t] - 512, shape))
        writes = []
        a1, d1 = lift(x0, "e1", "o1", "dp1", 10, o1, p1, "d1", "a1")
        writes += [("o1", x0)] if o1 else [("e1", x0)]
        vals = []
        if v1:
            writes.append(("dp1", d1))
            a2, d2 = lift(a1, "e2", "o2", "dp2", 11, o2, p2, "d2", "a2")
            writes += [("o2", a1)] if o2 else [("e2", a1)]
            q1 = f("q1", _quant(d1, 3))
            writes.append(("b0", q1))
            vals.append("b0")
            if v2:
                writes.append(("dp2", d2))
                a3, d3 = lift(a2, "e3", "o3", "dp3", 12, o3, p3, "d3", "a3")
                writes += [("o3", a2)] if o3 else [("e3", a2)]
                q2 = f("q2", _quant(d2, 2))
                writes.append(("b1", q2))
                vals.append("b1")
                if v3:
                    writes.append(("dp3", d3))
                    q3 = f("q3", _quant(d3, 2))
                    qa3 = f("qa3", _quant(a3, 1))
                    qp = 0 if p3 == 0 else rd("qa")
                    da3 = f("da3", _s(qa3 - qp, 13))
                    writes += [("b2", q3), ("b3", da3), ("qa", qa3)]
                    vals += ["b2", "b3"]
        if stale:
            out += [f("hv", _s(rd(r), 13)) if r in written else
                    np.full(shape, WILD, dtype=np.int64) for r in vals]
        for r, v in writes:                     # all at the clock edge
            wr(r, v)
            written.add(r)
        if not stale:                           # issued before the channel's next sample
            out += [f("hv", _s(rd(r), 13)) for r in vals]
    return np.stack(out, axis=-1) if out else np.zeros(shape + (0,), dtype=np.int64)


def self_check(x: np.ndarray) -> None:
    """Fault-free model == model/nlc/lossy.py channel_symbols on x (T, C)."""
    from nlc.lossy import LossyConfig, channel_symbols
    cfg = LossyConfig()
    got = simulate(x)[0]
    for c in range(x.shape[1]):
        ref = [v for b in range(x.shape[0] // BLOCK)
               for _, v in channel_symbols(x[b * BLOCK:(b + 1) * BLOCK, c], cfg)]
        if list(got[c]) != ref:
            j = next(i for i, (a, b) in enumerate(zip(got[c], ref)) if a != b)
            raise AssertionError(f"datapath model != lossy.py, channel {c} slot {j}")
