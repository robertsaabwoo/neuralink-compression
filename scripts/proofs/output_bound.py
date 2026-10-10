"""Worst-case proof of the output path (C-OVF-5): can any input data make the issuer fall a
whole frame behind (abort, nlc_lossy `pending > n_sel`) when the host meets its byte rate?

Model (clock by clock, one packet after another):
  issuer   a channel's burst of frame f (push_schedule: 0/1/2/4 symbols) can start once its
           sample has arrived; it is done when its last symbol is accepted by the coder.
           Deadline: done before that channel's NEXT sample arrives (else pending > n_sel).
           A 0-symbol burst still takes one issuer clock.
  coder    accepts a symbol every K clocks (K = 1 single-cycle divider, 2 = looped DIV_K = 5)
           when its output word register is empty; a symbol's word is at most B_SYM bytes.
  serial.  moves 1 byte per clock from the word register into the out FIFO (depth D) if not full.
  host     takes the FIFO head at most once every T clocks (T = clocks per byte, C-BW-3).
  packet   1 header byte before the first symbol, n_flush x state_bytes after the last burst
           (written through the word register, state_bytes per word).

Why this is a proof and not a test: every step is a max/plus of earlier event times, so the
completion time of every burst is monotone non-decreasing in the byte count of every symbol.
Giving EVERY symbol the per-symbol maximum (2 raw escape bytes + 2 renormalisation bytes,
nlc.rans_tdm: a 22-bit state renormalises at most 2 bytes) yields the latest completion any
data can produce. Coding order equals arrival order, so all channels in adjacent slots (zero
gaps, maximum bunching) is the worst slot placement: for any other placement each burst's
deadline moves later by at least as much as its start can. So: min slack >= 0 here
=> no data and no slot set can cause an abort. Modelling granularity is +-2 clocks per
stage; a pass is only claimed with slack well above that.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "model"))
from nlc.lossy import LossyConfig, push_schedule  # noqa: E402

B_SYM = 2 + 2          # esc_bytes raw + max renormalisation bytes (see docstring)
HEADER = 1             # nlc.packet: mode | seq
STATE_BYTES = 3        # ceil((prob_bits + lsh + 8) / 8) = ceil(22 / 8)


def run(n_ch=8, frame=256, gap=1, k=2, depth=8, t_host=4, packets=2, b_sym=B_SYM,
        n_flush=8, word_reg=True):
    """Return (min slack in clocks over all bursts, where, max FIFO fill)."""
    cfg = LossyConfig()
    sched = push_schedule(cfg) * cfg.blocks_per_packet          # symbols per frame of a packet
    fpp = len(sched)
    # work items in coding order: (arrival clock, n_symbols, bytes per symbol, deadline, label)
    items = []
    for p in range(packets):
        items.append((p * fpp * frame, 1, HEADER, None, f"p{p} header"))
        for f in range(fpp):
            for c in range(n_ch):
                arr = (p * fpp + f) * frame + c * gap
                items.append((arr, sched[f], b_sym, arr + frame, f"p{p} f{f} ch{c}"))
        end = ((p + 1) * fpp) * frame - frame + (n_ch - 1) * gap   # last sample of the packet
        items.append((end, n_flush, STATE_BYTES, None, f"p{p} flush"))
    t = 0
    coder_next = 0
    wr = 0                      # bytes left in the coder's output word register
    fifo = 0
    host_next = 0
    min_slack, where, peak = 10 ** 9, "", 0
    i, left = 0, None
    horizon = (packets * fpp + 4) * frame
    while i < len(items) and t < horizon:
        # host pops (it may take a byte every t_host clocks)
        if fifo and t >= host_next:
            fifo -= 1
            host_next = t + t_host
        # serialiser: word register -> FIFO, 1 byte per clock
        if wr and fifo < depth:
            wr -= 1
            fifo += 1
            peak = max(peak, fifo)
        # issuer / coder
        arr, nsym, bpb, dl, lab = items[i]
        if left is None:
            left = nsym
        if t > arr:
            if left == 0:                       # empty burst: one issuer clock
                done = True
            elif (wr == 0 or not word_reg) and t >= coder_next:
                wr += bpb
                coder_next = t + k
                left -= 1
                done = left == 0
            else:
                done = False
            if done:
                if dl is not None and dl - t < min_slack:
                    min_slack, where = dl - t, lab
                i, left = i + 1, None
        t += 1
    if i < len(items):
        return -10 ** 9, "did not finish", peak
    return min_slack, where, peak


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--frame", type=int, default=256)
    ap.add_argument("--n-ch", type=int, default=8)
    ap.add_argument("--d8", action="store_true",
                    help="D8 output (no FIFO, downstream takes 1 byte/clock): slack per frame length")
    a = ap.parse_args()
    if a.d8:
        # D8 = depth 1 (the word register's byte goes straight out), host 1 clock/byte.
        # C-IF-9: the guarantee is claimed at 256-clock frames (II=1, 256 slots).
        print(f"D8, worst case: every symbol {B_SYM} bytes, {a.n_ch} adjacent channels, 2 packets\n")
        print("| frame (clk) | coder clk/sym | min slack (clk) | tightest burst | verdict |")
        print("|---|---|---|---|---|")
        for frame in (256, 192, 128, 96, 64):
            for k in (1, 2):
                s, w, _ = run(n_ch=a.n_ch, frame=frame, k=k, depth=1, t_host=1)
                v = "PROVEN" if s >= 8 else ("marginal" if s >= 0 else "can abort")
                print(f"| {frame} | {k} | {s} | {w} | {v} |")
        return
    print(f"worst case: every symbol {B_SYM} bytes, {a.n_ch} channels in adjacent slots, "
          f"{a.frame}-clock frames, 2 packets\n")
    print("| coder clk/sym | FIFO depth | host clk/byte | min slack (clk) | tightest burst | peak FIFO | verdict |")
    print("|---|---|---|---|---|---|---|")
    for k in (1, 2):
        for depth in (1, 2, 4, 8, 16, 32, 64):
            for t_host in (1, 2, 3, 4):
                s, w, pk = run(n_ch=a.n_ch, frame=a.frame, k=k, depth=depth, t_host=t_host)
                v = "PROVEN" if s >= 8 else ("marginal" if s >= 0 else "can abort")
                print(f"| {k} | {depth} | {t_host} | {s} | {w} | {pk} | {v} |")


if __name__ == "__main__":
    main()
