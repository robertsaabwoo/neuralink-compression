# Results: measurements and findings

Numbers behind the summary in the [README](../README.md). Pre-layout, sky130_fd_sc_hd, typical
corner, 5 MHz, 8 channels unless stated. Raw per-run output: `reports/latest/` (`summary.md`,
`metrics.json`) and one JSON per `test/core` test in `test/results/core/`. Last updated 2026-10-07.

## Findings (input to the design phase)

F2-F4 are fixed (2026-10-08): D5-D7 are implemented and their tests pass (T-OVF-1/2, T-IF-3b, T-ROB-2/3/4); `KNOWN_FAIL` is empty.

The tests that expose F2-F4 stay as written and fail on today's RTL. `scripts/flow/flow.py`
lists them in `KNOWN_FAIL`, so they are reported without failing the run.

| # | finding | test | constraint |
|---|---|---|---|
| F1 | **Processing latency is 2.0 ms, not ~310 us.** A sample is fully coded only when the last symbol that depends on it is coded. Through the lifting updates and the delta-coded a3, a sample early in a 64-frame block reaches a symbol 36 frames later. Time in the queue alone is 307 us | T-IF-1 | C-LAT-1 (definition open) |
| F2 | **Disable mid-packet leaves an unterminated packet at the host.** The host already has the header and first bytes; the next run's packet 0 is appended (668 = 15 + 653 bytes). Clearing the FIFO would not help: the pins need an abort signal | T-IF-3b, T-ROB-2, T-ROB-3 | C-IF-6/7 |
| F3 | **A host stall corrupts that packet and every later one, with no recovery.** The core `overflow` pin stays 0. A 300-clock stall during the flush is absorbed | T-OVF-1/2/3 | C-OVF-1..3 (D3) |
| F4 | **One short frame misaligns the channels for good.** Long frames and a missing `s_frame` recover | T-ROB-4 | C-IF-8 |
| F5 | **Worst-case host speed: one byte every 4 clocks (0.8 us)** since the eager drain (was 11): worst-case frames carry up to 101 bytes (was 45). LFSR average 21.4 bits/sample (3.36 Mbit/s) unchanged; real data is smoother than before (peak 30 bytes/frame, was 40). Accepted 2026-10-08 (C-BW-3) | T-BW-1/2 | C-BW-2/3 |
| F6 | Random power-up contents in all 234 no-reset registers: identical output | T-ROB-5 | C-FN-6 |
| F7 | Power was 98% flip-flop clock pins (618 of 629 uW, also when idle). Fixed by clock gating (F11) | T-PWR-1 | C-PWR-1/2 |
| F8 | Critical path 61.9 ns at ss: fine at 200 ns, fails TT's old 20 ns default | T-STA-1 | C-TIM-1 |
| F9 | Yosys and Icarus disagreed on signed SystemVerilog (7,033 vs 670 bytes); fixed with explicit bit slices, now a coding rule | T-GL-1 | C-RTL-2 |
| F11 | **Two-level clock gating: 628.5 -> 9.5 uW op, 618 -> 0.8 uW idle, 128k -> 77k um^2.** 159 `dlclkp` gates; per-channel rows, FIFO slots and coder registers only see an edge when written, parent gates only when their block has work, async clear of the control state so `enable` = 0 stops all clock edges. Gating also removes the enable muxes (`edfxtp` 30 -> `dfxtp` 20 um^2) | T-PWR-1, T-GL-1/2 | C-PWR-1/2 |
| F12 | The TT top was the power bottleneck and unmeasured: 216 flops outside the lossy core clocked every cycle (~50 uW estimated). Fixed: gated (F11 pattern) and measured at system level (T-PWR-2, F14) | T-PWR-2 | C-PWR-1 |
| F13 | **Eager FIFO drain (format change): the coder takes each burst as soon as it is pushed** (frame, channel, push order) instead of one symbol per channel per sample. Per-channel storage 8 -> 4 entries in fixed slots, no ring pointers: 2,127 -> 1,671 flops, 76.9k -> 62.2k um^2. Packet sizes identical (order only); processing latency 1,997 -> 1,846 us; T-IF-3b now passes (nothing of the next packet is out when it disables) | T-CHG-7, all | C-AREA, C-LAT-1 |
| F15 | **Second gating/area round:** slot selector split gating, issuer operand isolation on empty-burst frames, a lossy grandparent gate, children under parent gates (idle), burst slots narrowed to 8/10/11/13 bits, serialiser reads the coder's word in place (no `sbuf`). Lossy core 62.2k -> 59.2k um^2, 1,671 -> 1,573 flops, op 9.34 -> 8.07 uW, worst 11.1 -> 8.7 uW, idle 0.77 -> 0.16 uW; host requirement unchanged (4) | all | C-PWR, C-AREA |
| F16 | Gate-level only: the output FIFO's pointers stayed X after reset (`m_valid` = X, TT top and `nlc_core` gate-level runs failed) while RTL with the real gate model passed. A synchronous reset synthesised as `!((Q & r) or !(Q or r'))` is 0 in reset, but the cell models cannot resolve the reconvergent X. Fixed with asynchronous resets (C-RTL-3) | T-GL-2, T-PWR-2 | C-RTL-3 |
| F14 | **System power measured (`nlc_core` gate level, real interface): 15.9 uW op, 2.5 uW idle** (target 16 / 2) | T-PWR-2 | C-PWR-1/2 |
| F10 | **The coder pipeline was 547 of the 2,681 flops for nothing.** The coder sees at most 8 symbols per 256 clocks. With the 10-step divider combinational (`DIV_REG` = 0), there are 2,134 flops, 110 ns slack at ss / 200 ns, and a deepest path of 121 cells (< 200 for TT's unit-delay gate sim). Output bytes are identical. The other ~1,950 flops are per-channel state, which re-timing can't remove | sweep below | C-TIM-1/4 |
| F17 | **The routed chip draws 80 uW running and 51 uW idle (T-PWR-3), not ~12-17 uW: the clock tree CTS built is 48 / 39 uW of it.** The pre-layout netlist of the same TT top on the same real-data scenario: 16.8 / 6.0 uW. Run `a9c2329`. The always-running trunk is 36 `clkbuf_16`, 14 deep, ~1.1-1.5 uW each, mostly `delaybuf_*_clk`. CTS pads the ungated branch to match the insertion delay of the gated branches. That branch is the 18 TT pin input flops of `project.v` (`uio_q`, `strobe_q`, `ack_q`, `cfg_*`, `s_data`, `s_frame`) plus 8 top-level clock gates. Below the 143 gates, 535 more clock buffers (almost all `clkbuf_16`) serve a median of 11 flops per gate (~8 uW running). Clock buffers are 14.0k um^2 (12% of cell area). Levers (design phase, ask first): CTS buffer list / skew target in the TT config, fewer and larger gate groups, keep ungated flops next to the clock root. Repair buffers 5.0 uW, hold buffers 2.4 uW | T-PWR-3 | C-PWR-1/2 |
| F18 | **High-fanout selects: the resizer buffers them with `clkdlybuf4s25_1` delay cells, so edges reach 2.3 ns at ss (1.46 at tt) against 0.75 ns.** 905 tree buffers on 432 nets, 8.7k um^2, of which most are `clkdlybuf4s25_1`. Largest: the decoded channel select from `core.smp_ch[1:0]` (4 nets, 98-128 sinks, 13-17 buffers, 4-6 deep) and `core.smp_ch[2]` (103), `rst_n` (105), the lossy round-robin decode from `u_lossy.rr` / `rr_iss` (58-69 each), `u_lossy.clr_n` (63), `u_rans.f_ch` (49). The slowest edges are on `smp_ch[0]` / `smp_ch[1]` themselves (26 sinks behind 3 delay cells each). 3,283 pins over the limit at ss and 366 at tt, the same counts as LibreLane: not a precheck failure, but slow edges cost short-circuit power. Full table: `reports/latest/layout_fanout.md` | T-FAN-1 | C-PWR, C-AREA |
| F19 | **What makes the CTS delay buffers: OpenROAD's latency balancer evens out clock arrival over all flops under `clk`, padding the shallow branches.** OpenROAD treats a `dlclkp` as a sink with insertion delay (like a macro). `LatencyBalancer` (log `CTS-0033 Balancing latency`, `CTS-0036 inserted N delay buffers`) then adds delay buffers where a branch with few gate levels splits off from deeper gated branches. In a9c2329 all 56 sat on the six nets that drive both gates and flops: `clk` (the 18 TT pin flops of `project.v` + 8 gates: 29, the always-on 39 uW trunk), `u_lossy.clk_l` (one flop, `skip`: 18), `clk_s` (3), `u_rans.clk_c` (3), FIFO clock (2), `u_sel.gclk_h` (1). Moving the pin flops behind an always-on gate (`rtl-pins`) leaves `clk` with gates only, yet 28 delay buffers stay on its trunk: the pin branch is still one gate level against the core's 2-4, so the balancer is global, not per mixed net. Preview, small buffers + hold margin 0.3: 41.2/21.2 -> 37.0/17.2 uW (running/idle), trunk 52 -> 31 buffers. Doing it on all six nets (`rtl-all`, 16.5 uW idle) produced X on `m_valid` in the routed unit-delay simulation (STA hold met at all corners), so it was dropped. Settings tried with no effect: `CTS_DELAY_BUFFER_DERATE_PCT` 0 (applies to hard macros, not this balancer), `CTS_SINK_CLUSTERING_SIZE` 24, a delay cell (`clkdlybuf4s50_1`) in `CTS_CLK_BUFFERS` (it pads with `clkbuf_2` only). The pass is off only with `clock_tree_synthesis -no_insertion_delay`, which LibreLane 3.0.14's `cts.tcl` cannot pass | T-PWR-3 (cts_preview), CTS log, OpenROAD src/cts | C-PWR-1/2 |
| F20 | **Clock tree and TT-pin power after layout: 80.0/51.4 -> 34.8/9.5 uW (running/idle), core 0.80 uW idle.** All rows routed, real data (T-PWR-3), signed off (gds, gl_test, precheck), bit-exact; core share from the T-PWR-3 split (core / TT pin glue / shared clock trunk). a9c2329 80.0/51.4 (core 28.5/1.98); `cts-a-hold` (CTS root `clkbuf_2`, tree `clkbuf_4/2/1`, `PL_RESIZER_HOLD_SLACK_MARGIN` 0.3) 47.7/25.3 (22.6/1.61); `rtl-pins` (pin registers behind an always-on gate) 41.9/19.5 (22.6/1.84); `noid` (latency balancer off: patched LibreLane image, not TT-standard; 0 delay buffers, trunk 3 buffers) 36.4/14.3 (22.2/1.78); E1 (pin data registers load only on strobes of selected slots, `s_want` from the slot selector) 32.9/10.9 (21.2/1.49); E3 (one gate in front of the whole core) 38.1/13.5 (24.8/1.78): on its own not worth it; **E1+E3 34.8/9.5 (23.8/0.80)**, hold +0.187 ns. Pre-layout T-PWR-2 `nlc_core`: 12.5/1.07. `tt-best` = the E1+E3 RTL with TT's standard flow (balancer on). Not useful: `CTS_BALANCE_LEVELS` (no change), three-deep pin gates (more padding), a one-hot channel select (same sink count as the 8:1 read muxes) | T-PWR-3, cts_preview | C-PWR-1/2 |

## Future work: lower supply voltage (noted 2026-10-09)

Measured on the routed E1+E3 design (real data, sky130 ss -40C libraries, same activity):
1.76 V 32.6 uW, 1.60 V 26.4 uW, **1.40 V 18.9 uW (-42%, still meets 5 MHz at the slow corner,
+31 ns slack)**, 1.28 V 15.0 uW (fails 5 MHz). The timing slack of the 8-of-256 schedule is
worth most as voltage.

On TT the only route is a bench-supplied core on the analog rail, as in Matt Venn's
[adjustable supply digital counter](https://github.com/mattvenn/adjustable-psu-digital-counter)
(TTCAD25a: a digital macro on VAPWR, the board's analog supply fed from a variable PSU). An
on-chip LDO does not pay: published TT LDOs draw 19-74 uA quiescent (34-133 uW), more than this
chip. Preconditions for a future iteration:
1. Area: fit an analog 2x2 slot (~334 x 225 um): ~45-50k um^2 of cells (today ~102k in 4x2).
2. Interface: level shifters on the 1.8 V pin signals, or an interface that fits the 6 analog
   pins (< 500 ohm, 4 mA each).
3. Flow: harden the core as a macro on VAPWR and place it in the analog template by hand
   (TT's flow builds one power domain); VAPWR is the chip's shared analog rail, set on the board.

## Budgets

| metric | value | limit / target | status |
|---|---|---|---|
| lossy core area | 62,200 um^2 (1,671 flops + 127 clock gates) | 173,000 / 86,600 | PASS |
| TT design utilisation (8x2) | 48% | 70% / 60% | PASS |
| setup slack, ss, 200 ns | +138 ns | >= 0 / 60 | PASS |
| power, real data | lossy core 9.3 uW (was 628.6); `nlc_core` 15.9 uW | 40 / 16 | PASS |
| power, idle | lossy core 0.77 uW (was 617.8); `nlc_core` 2.5 uW | 10 / 2 | PASS / WARN |
| processing latency | 2.0 ms | 1 / 0.41 ms | FAIL (F1) |
| delivery latency | 13.4 ms | 40 / 20 ms | PASS |
| bits/sample, 160 held-out files | 2.087 | 2.2 / 2.1 | PASS |
| median SNR | 18.0 dB (10th pct 13.3) | 17 / 18 dB | PASS |

## Test status

| suite | result |
|---|---|
| model (pytest, incl. change guard) | 76 pass |
| `test/core` (47 cases, ~25 min) | F2-F4 cases fail as intended; T-ROB-4 long/missing and T-ROB-7 (reset, 3 points) pass |
| `test/lossy` (incl. power scenarios) | 7 pass |
| `test/rans` | 6 pass |
| TT top through the pins, 200 ns | lossy pass; modes 0/2/3 skipped (D1); replay 7/7 |
| gate level: lossy core, TT top netlist | pass, pass |

## Data

**Bandwidth, real data** (T-IF-1, 4 packets): packets 670, 650, 637, 653 bytes; 2.55 bits/sample
on this excerpt, SNR 17.6 dB; out FIFO peak 1; flush 27 bytes in 55 clocks.
Bytes per frame (count of frames): 1: 207, 2: 265, 3: 182, 4: 124, 5: 87, 6: 30, 7: 11, 8: 2,
9: 2, 26-40 (flush frames): 4.

**Bandwidth, worst case** (T-BW-1 LFSR): up to 45 bytes/frame, typically 18-24; flush 30 bytes.

**Host turnaround search** (T-BW-2, LFSR, clocks per byte -> no loss?): 33 no, 17 no, 9 yes,
13 no, 11 yes, 12 no. Result 11, then verified for 2 packets.

**Latency** (T-IF-1): queue (push -> coder) median 103 us, max 307 us; processing (incl.
look-ahead) median 1,664 us, max 1,997 us; delivery 13.4 ms; look-ahead up to 36 frames.

**Power after clock gating** (2026-10-08, gate level, op = 2 packets from frame 64, idle =
`test_power_idle`): op 9.55 uW (sequential 2.72, combinational 4.92, clock gates 1.90), with
glitches 9.69; idle 0.77 uW. Per cell type: flops ~2.0, the 4 parent gates ~1.2 (`u_cg_s`
alone 0.6: its GCLK drives 144 child gates), 152 child gates ~0.9, data path ~5 uW.

**Power before** (gate level, 2 packets from frame 64): op 628.65 uW (sequential 621.5, combinational
7.15); with glitches 628.94; idle 617.83.

**Algorithm** (`algo_eval.py --quick`, 4 held-out groups): 2.08 bits/sample, median SNR 16.6 dB
(subset, reads lower than the 160-file run), spike hit rate 93.9%, false positives 20.6% of
original spikes, max reconstruction error 13 codes, held-out rate 0.7% worse than training.

**Coverage** (docs/testing.md 4.5): 40 of 43 bins. Missing: `seq_wrap` (needs `NLC_LONG=1`),
`occ_7` and `back_to_back_same_channel` (look unreachable as sampled; to decide).

**Coder divider registers** (`DIV_REG` of `nlc_lossy`/`nlc_rans`; bit k = register after op k,
op 0 = renorm, op 1-10 = quotient bits; lossy core, flattened, ss / 200 ns, hold ff):

| `DIV_REG` | divider regs | flops | setup slack | hold | deepest path (cells) |
|---|---|---|---|---|---|
| 7ff (old) | 11 | 2,681 | 109.6 ns | 0.242 ns | 63 |
| 555 | 6 | 2,426 | 108.1 | 0.242 | 60 |
| 421 | 3 | 2,273 | 106.7 | 0.242 | 61 |
| 021 | 2 | 2,235 | 105.9 | 0.242 | 62 |
| 001 | 1 | 2,184 | 111.3 | 0.242 | 102 |
| **000 (now)** | 0 | **2,134** | 109.9 | 0.242 | 121 |
