"""Bit-exact model of nlc_lossy's per-channel datapath, with fault hooks (bring-up only).

Mirrors src/nlc_lossy.sv + src/nlc_lift53.sv signal by signal, at the RTL widths: the sample
row, ONE shared 12-bit lifter driven by the issuer (kk = 0/1/2: level 1/2/3, kk = 3: the a3
delta from xr), the staging registers that feed the latch rows, the quantisers. A
hypothesised hardware fault is simulated and compared against the symbols decoded from a
captured packet. Fault-free it equals model/nlc/lossy.py channel_symbols (self_check()).

Vectorised over a batch of hypotheses: every row is an array of shape (H, C) (H hypotheses x
C channels), so e.g. all 4096 power-up values of a stuck 12-bit row, or every stuck-at bit
of every shared net, run in one pass.

Faults (all optional):
  stuck  {row: (on (H,C) bool, value (H,C) int)}   row never written (latch row's clock gate
                                                   dead): holds value (power-up) where on
  rowbit {row: (and_mask, or_mask)}                stuck bit(s) in a row's latches (on read)
  sig    {net: (and_mask, or_mask)}                stuck bit(s) on a shared net / register

Rows (per channel, g_ch[c], nlc_rreg -> latch rows): sx e1 o1 dp1 e2 o2 dp2 e3 o3 dp3 qa.
Shared nets: x0 (centred sample), sg_sx (sample staging), lx le lo ldp (lifter inputs), la ld
(lifter outputs, every level), q1 q2 q3 qa3 (quantisers), da3 (a3 delta), xr (the register
between levels), hv (issued value), sg_xq sg_e2 sg_e3 sg_dp (row staging registers).
"""

from __future__ import annotations

import numpy as np

ROW_W = {"sx": 10, "e1": 10, "o1": 10, "dp1": 11, "e2": 11, "o2": 11, "dp2": 12, "e3": 12,
         "o3": 12, "dp3": 13, "qa": 12}
SIG_W = {"x0": 10, "sg_sx": 10, "lx": 12, "le": 12, "lo": 12, "ldp": 13, "la": 13, "ld": 13,
         "q1": 13, "q2": 13, "q3": 13, "qa3": 13, "da3": 13, "xr": 13, "hv": 13,
         "sg_xq": 12, "sg_e2": 11, "sg_e3": 12, "sg_dp": 13}
BLOCK = 64
WILD = 1 << 20                      # a symbol that depends on unknown (power-up) contents
CTX_OF_KK = (1, 2, 3, 0)            # issuer step kk -> context (d1, d2, d3, a3)


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


def frame_sched(n: int):
    """The issuer's global pair schedule for frame n of a block (nlc_lossy f, f1..f3, pv1..3)."""
    f = n & 63
    pv1 = (f == 63) if f & 1 else f != 0
    f1 = (f >> 1) if f & 1 else ((f >> 1) - 1) & 31
    pv2 = pv1 and ((f1 == 31) if f1 & 1 else f1 != 0)
    f2 = (f1 >> 1) if f1 & 1 else ((f1 >> 1) - 1) & 15
    pv3 = pv2 and ((f2 == 15) if f2 & 1 else f2 != 0)
    f3 = (f2 >> 1) if f2 & 1 else ((f2 >> 1) - 1) & 7
    nb = 4 if pv3 else 2 if pv2 else 1 if pv1 else 0
    return f, f1, f2, f3, nb


_SCHED = [frame_sched(n) for n in range(BLOCK)]


def push_slots():
    """Symbols of one block in production order: list of (frame in block, kk)."""
    return [(n, kk) for n, s in enumerate(_SCHED) for kk in range(s[4])]


def simulate(x: np.ndarray, H: int = 1, stuck=None, rowbit=None, sig=None,
             stale: bool = False, x_prev=None) -> np.ndarray:
    """x: (T, C) ADC codes, T a multiple of 64 (whole blocks, e.g. one packet).
    Returns (H, C, J) symbol values (signed, as issued) in production order.
    stale: the issuer reads the sample row *before* the channel's sample of the frame writes
    it (a sample delivered under the wrong channel index: that channel codes the previous
    frame's sample); the row's content before frame 0 is x_prev ((H, C) codes, or None:
    power-up, unknown -> symbols that depend on it are WILD)."""
    stuck, rowbit, sig = stuck or {}, rowbit or {}, sig or {}
    x = np.asarray(x, dtype=np.int64)
    T, C = x.shape
    if stale and x_prev is None:                # find the symbols that depend on power-up
        probe = np.array([0, 1023, 300, 777], dtype=np.int64)[:, None]
        v = simulate(x, H=4, stale=True, x_prev=np.broadcast_to(probe, (4, C)))
        dep = (v != v[:1]).any(axis=0)
        out = np.broadcast_to(np.where(dep, WILD, v[0]), (H,) + v.shape[1:]).copy()
        return out
    shape = (H, C)
    rows = {r: np.zeros(shape, dtype=np.int64) for r in ROW_W}
    if stale:
        rows["sx"] = _u(np.broadcast_to(np.asarray(x_prev, dtype=np.int64) - 512, shape), 10)
    for r, (on, val) in stuck.items():
        rows[r] = np.where(on, _u(np.broadcast_to(val, shape), ROW_W[r]), rows[r]).astype(np.int64)

    def rd(r):                                  # row read: sign-extended, with stuck bits
        v = rows[r]
        if r in rowbit:
            a, o = rowbit[r]
            v = (v & a) | o
        return _s(v, ROW_W[r])

    def wr(r, v):
        v = _u(np.broadcast_to(v, shape), ROW_W[r])
        if r in stuck:
            v = np.where(stuck[r][0], rows[r], v)
        rows[r] = v

    def f(name, v):                             # shared net with stuck bits
        if name in sig:
            w = SIG_W[name]
            a, o = sig[name]
            v = _s((_u(v, w) & a) | o, w)
        return v

    def lift(xin, e, o, dp, odd, first):        # nlc_lift53 #(.W(12)), d / a nets ld / la
        x12, e12, o12 = f("lx", _s(xin, 12)), f("le", _s(e, 12)), f("lo", _s(o, 12))
        dp13 = f("ldp", _s(dp, 13))
        e_nx = e12 if odd else x12
        o_cur = x12 if odd else o12
        s2 = _s(e12 + e_nx, 13)
        d = f("ld", _s(o_cur - (s2 >> 1), 13))
        d_prev = d if first else dp13
        a = f("la", _s(e12 + ((d_prev + d + 2) >> 2), 13))
        return x12, a, d

    out = []
    for t in range(T):
        fr, f1, f2, f3, nb = _SCHED[t % BLOCK]
        sg_sx = f("sg_sx", f("x0", np.broadcast_to(x[t] - 512, shape)))
        if not stale:
            wr("sx", sg_sx)
        # kk = 0: level 1 on the stored sample (also in empty-burst frames: store only)
        lx, la, ld = lift(rd("sx"), rd("e1"), rd("o1"), rd("dp1"), fr & 1, f1 == 0)
        w = [("o1" if fr & 1 else "e1", f("sg_xq", _u(lx, 10)))]
        if nb:
            out.append(f("hv", f("q1", _quant(ld, 3))))
            w.append(("dp1", f("sg_dp", ld)))
            if nb == 1:
                w.append(("o2" if f1 & 1 else "e2", f("sg_e2", la)))
            xr = la
        if nb >= 2:                             # kk = 1: level 2 on a1 (from xr)
            for r, v in w:
                wr(r, v)
            lx, la, ld = lift(f("xr", xr), rd("e2"), rd("o2"), rd("dp2"), f1 & 1, f2 == 0)
            out.append(f("hv", f("q2", _quant(ld, 2))))
            w = [("o2" if f1 & 1 else "e2", f("sg_e2", lx)), ("dp2", f("sg_dp", ld))]
            if nb == 2:
                w.append(("o3" if f2 & 1 else "e3", f("sg_e3", la)))
            xr = la
        if nb == 4:                             # kk = 2: level 3 on a2, then the a3 delta
            for r, v in w:
                wr(r, v)
            lx, la, ld = lift(f("xr", xr), rd("e3"), rd("o3"), rd("dp3"), f2 & 1, f3 == 0)
            out.append(f("hv", f("q3", _quant(ld, 2))))
            qa3 = f("qa3", _quant(la, 1))
            da3 = f("da3", _s(qa3 - (0 if f3 == 0 else rd("qa")), 13))
            w = [("o3" if f2 & 1 else "e3", f("sg_e3", lx)), ("dp3", f("sg_dp", ld)),
                 ("qa", f("sg_xq", qa3))]
            out.append(f("hv", _s(f("xr", da3), 13)))       # kk = 3: delta a3 from xr
        for r, v in w:
            wr(r, v)
        if stale:
            wr("sx", sg_sx)
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
