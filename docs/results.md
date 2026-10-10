# Results: measurements and findings

Numbers behind the summary in the [README](../README.md). Pre-layout, sky130_fd_sc_hd, typical
corner, 5 MHz, 8 channels unless stated; "routed" and "preview" numbers come from GitHub
`gds` / `cts_preview` runs (run ids given). Raw per-run output: `reports/latest/` (`summary.md`,
`metrics.json`) and one JSON per `test/core` test in `test/results/core/`. Last updated 2026-10-10.

## Findings (input to the design phase)

F2-F4 are fixed (2026-10-08): D5-D7 are implemented and their tests pass; `KNOWN_FAIL` is
empty. Since D8 (2026-10-09, valid-only output) the host-stall findings F3 and F5 are history:
the output cannot be stalled, and the throughput guarantee is C-IF-9 (F25). F1 (latency
definition) is resolved (C-LAT-1 restated 2026-10-10). Open: F27 (OpenROAD CTS bug; worked around).

| # | finding | test | constraint |
|---|---|---|---|
| F1 | **Processing latency is 1.85 ms, not ~310 us, and that is the algorithm.** A sample is fully coded only when the last symbol that depends on it is coded. Through the lifting updates and the delta-coded a3, a sample early in a 64-frame block reaches a symbol 36 frames later (1,843 us of waiting for samples; queue alone 307 us). Measured 1,997 us before the eager drain (F13), 1,846 us since. Not observable at the pins (packets decode whole, C-LAT-2). **Resolved 2026-10-10:** the challenge's "< 1 ms" is read as throughput (C-IF-9); C-LAT-1 is now the derived bound 2,253 us | T-IF-1 | C-LAT-1 |
| F2 | **Disable mid-packet leaves an unterminated packet at the host.** The host already has the header and first bytes; the next run's packet 0 is appended (668 = 15 + 653 bytes). Clearing the FIFO would not help: the pins need an abort signal | T-IF-3b, T-ROB-2, T-ROB-3 | C-IF-6/7 |
| F3 | **A host stall corrupts that packet and every later one, with no recovery.** The core `overflow` pin stays 0. A 300-clock stall during the flush is absorbed | T-OVF-1/2/3 (retired by D8) | C-OVF-1..3 (D3) |
| F4 | **One short frame misaligns the channels for good.** Long frames and a missing `s_frame` recover | T-ROB-4 | C-IF-8 |
| F5 | **Worst-case host speed: one byte every 4 clocks (0.8 us)** since the eager drain (was 11): worst-case frames carry up to 101 bytes (was 45). LFSR average 21.4 bits/sample (3.36 Mbit/s) unchanged; real data is smoother than before (peak 30 bytes/frame, was 40). Accepted 2026-10-08 (C-BW-3). **Retired by D8:** no host turnaround exists any more; worst-case bytes/frame now feed C-IF-9 (F25) | T-BW-1/2 | C-BW-2/3 |
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
| F21 | **What did not lower power (2026-10-09), all bit-exact:** coder divider pipelined (`DIV_REG` one stage +22%, two stages +51% lossy-core power; glitches are only ~4%); non-restoring divider (+1% power, +2% coder area: the per-bit add/subtract XORs cost as much as the restore multiplexers); slot selector countdown (`sA`, +0.1/+0.4 uW running/idle), Gray-coded counter (`sC`, no change), 2D counter with gated high half (`sB`, fails the routed unit-delay simulation): the slot counter's clock is only 0.71 uW. Pin control flops on a change-detect gate (`e13c`) and placement density 75 also fail the routed unit-delay simulation: these failures were blamed on races of the unit-delay gate model (a lost row write in the encoder, an ack popping the FIFO twice); **corrected by F28: each of the three post-CTS netlists has a clock gate without CLK (the OpenROAD CTS bug of F27), a real netlist defect, not a race**. **What did:** smaller rANS renormalisation units cost no compression (held-out real data, round trip exact): bytes (today, 10 divider steps) 2.0245 bits/sample, nibbles 6 steps -0.32%, 2 bits 4 steps -0.46%, single bits 3 steps -0.50% (smaller end-of-packet flush); a format change (model, decoder, golden vectors), not done yet | DIV_REG sweep, previews, renorm sweep | C-PWR-1, C-ALG |
| F22 | **Area round (2026-10-09/10), TT-top synth 68.3k (e13) -> 48.5k um^2, all bit-exact, format unchanged.** On top of e13: B shared lifter, the issuer lifts one level per issued symbol on one 12-bit lifter, burst buffers gone (-9.2k, +0.7 uW); G flow: LibreLane 3.0.14 excluded every drive-1 cell (+14.5k; fixed with `SYNTH_EXCLUDED_CELL_FILE`), plus hold margins, fanout/slew limits, no input buffers, no CTS buffers below small gates (G2 signed off, run 38005154030); C looped coder divider, 5 quotient bits per clock (-2.8k); glue: slot registers as gated storage without reset, one slot mux (-1.1k); D8: output FIFO removed; S3: per-channel rows in latches (`nlc_lreg`, -4.3 to -5.0k, +1.2 uW). Dropped: H channel ring (-2.9k synth but +11.9k after CTS: 2,369 hold cells on the Q->D ring, 44.6 uW), S2 coarser gating (best -111 um^2 after layout). Final RTL (stack + CDC/DFT, a0966c0): 48,518 um^2, 337 flops, 126 clock gates, ss setup +129.0 ns, ff hold +0.187 ns | synth, STA, all suites | C-AREA-1..3 |
| F23 | **8 channels do not route in a TT 2x2 (core ~72.6k um^2).** The stack at 2x2 (post-CTS utilisation 0.784) and the brackets S1 + glue (0.826) and S1 (0.846) were still in detailed routing with ~122k-135k violations when GitHub stopped them at 6 h (runs 38018634952, 38016933967, 38016938609). S1 at 4 channels (0.568) routed and passed precheck (38016930331). Reducing to 4 channels was rejected (D10). Signed off (gds, gl_test, precheck): stack 3x2 (38025552897, util 0.537), pre-DFT all-in 3x2 (38025684427, util 0.542) and 4x2 (38025693891, util 0.409). The stack at 4x2 routed with 0 DRC but failed DisconnectedPins (F27) | GitHub `gds` | C-AREA-3/5 |
| F24 | **Latch rows hung LibreLane's post-CTS setup repair.** All 1,168 latch D pins have setup slack exactly 0 (time borrowing: the staging flops launch on the edge that opens the latch), so `repair_timing` chases them forever; margin 0 also hangs (routed parasitics tip 0 slightly negative). Fix: a place-and-route SDC (`src/pnr.sdc`, `PNR_SDC_FILE`) = TT's SDC + `set_false_path -setup` to the latch D pins; the sign-off SDC is unchanged and still times them. Worst real setup path +121 ns; the post-CTS repair step now takes ~3 min | OpenSTA pre-check, `cts_preview` | C-TIM-1 |
| F25 | **Throughput with the valid-only output (D8) is bounded by 1 byte/clock, not by the coder.** `scripts/proofs/output_bound.py` (no FIFO, 1 byte/clock, every symbol 4 bytes, 8 adjacent channels: 128 bytes/frame worst case), minimum slack in clocks: 256-clock frames +138, 192 +74, 128 +10 (inside the model's +-2 clocks/stage), 96 -54, 64 -118; a single-cycle divider gives the same numbers. So no data can abort a packet at the real rate (II = 1, 256 clocks/frame, D9), while worst-case data can at the TT pin path's 64-clock test frames (real data does not). Measured worst case (LFSR, T-BW-1): 101 bytes/frame. RTL check: T-BW-3 | proof, T-BW-1 | C-IF-9, C-OVF-5 |
| F26 | **Pin CDC and test access (a0966c0): synth 47.5k (stack) -> 48.5k um^2, ~+2.3 uW idle.** 2-flop reset synchroniser (recovery slack 160.5 ns from the pin, 196.6 ns from the synchroniser), 2-flop synchronisers on `s_strobe` / `cfg_en`, sample bus loaded by a gate whose enable comes from flops only, registered outputs (+1 clock latency), sticky overflow on `uio[4]`, config readback on `uo_out` (~630 um^2 of the total). The idle cost is the always-clocked pin flops (preview, synthetic data) | synth, STA, `cts_preview` | C-IF-11/12, C-PWR-2 |
| F27 | **OpenROAD CTS left one clock gate without CLK on the final RTL; placement density 64 avoids it.** CTS reports 101 sinks on the gated clock `core.u_enc.u_lossy.clk_i` and finishes with 100; the missing sink is a `g_ch[7]` `dlclkp` (`u_qa`, `u_o1`, `u_o3` or `u_dp2` depending on placement) whose CLK pin is unconnected in CTS's own output netlist. Failing: the final RTL at 2x2 and at 3x2 with `PL_TARGET_DENSITY_PCT` 60 (also with `CTS_SINK_CLUSTERING_ENABLE` false, and with timing- or routability-driven placement off), and the stack at 4x2. Placement sweep (nightly 38059238643): density 56, 64, 68, 72 and 76 keep all 126 clock gates connected; 64 chosen (post-CTS hold +0.251 ns, the best of the clean variants). The flow is deterministic for a given placement, so a clean preview means a clean sign-off CTS. Root cause in OpenROAD not found; the layout gate-level test and LibreLane's DisconnectedPins check catch it, so it cannot reach silicon unnoticed | `cts_preview`, nightly sweep, CTS log, post-CTS netlist | C-FN-5, C-AREA-5 |
| F28 | **SDF gate level (T-GL-3, 2026-10-10): the routed final design is bit-exact with real delays at every corner, 0 timing-check violations; F21's "unit-delay races" were F27.** `sdf_sim` workflow: LibreLane's routed netlist and SDF from a `gds` run, sky130 models with specify blocks (no `FUNCTIONAL`/`UNIT_DELAY`), `$sdf_annotate`, TT-top tests through the pins at 200 ns, inputs moved on the falling edge (as a host would; `rst_n` and `cfg_en` used to change on the rising edge). Simulators: OSS CVC (built from source) runs the timing checks; Icarus 13 (built, Ubuntu's 12 has no SDF INTERCONNECT) annotates delays but has no timing checks, so it is the functional cross-check. Final candidate c8fd3b5 (gds 38060355759): `test_lossy` bit-exact, readback and overflow-pin tests pass (2 RTL-only tests skip) at nom tt (CVC + Icarus), nom ss, nom ff, max ss and min ff (CVC); 0 $setuphold/$recrem/$width violations; clock pin -> output pin 1.27-1.72 ns (tt), 2.29-3.22 (ss), 0.83-1.15 (ff); the same run's unit-delay `gl_test`, `gds` and precheck pass. The checks are live: `test_sdf` sweeps `cfg_en` through the clock edge and CVC flags the synchroniser flop (2-8 $setuphold violations per corner). Pre-DFT all-in 3x2 (85ecfbe, gds 38025684427): `test_lossy` bit-exact, 0 violations (its readback/overflow tests fail: that netlist predates the DFT pins). F21 re-checked on the `cts_preview` netlists: sB (`g_ch[1].u_qa`), e13c (`g_ch[2].u_e2`, `g_ch[6].u_b0`) and density 75 (`g_ch[7].u_dp3`, its gds run 37876767874 stopped at LibreLane's DisconnectedPins) each have a `dlclkp` with no CLK pin; the passing sA and sC have none. A gated row whose gate never clocks loses its writes: the F21 symptom. Not SDF-simulated (previews have no SDF) | `sdf_sim` 38065565877, 38066696819, 38066323020, 38066329173, 38066705070, 38066707296; post-CTS netlists | C-FN-5, C-TIM-1 |

## Future work: lower supply voltage (noted 2026-10-09; not for this submission)

Decided 2026-10-10 (D10): this submission is a digital TT design at 1.8 V; the analog-slot
route below stays a roadmap item only.

Measured on the routed E1+E3 design (real data, sky130 ss -40C libraries, same activity):
1.76 V 32.6 uW, 1.60 V 26.4 uW, **1.40 V 18.9 uW (-42%, still meets 5 MHz at the slow corner,
+31 ns slack)**, 1.28 V 15.0 uW (fails 5 MHz). The timing slack of the 8-of-256 schedule is
worth most as voltage.

On TT the only route is a bench-supplied core on the analog rail, as in Matt Venn's
[adjustable supply digital counter](https://github.com/mattvenn/adjustable-psu-digital-counter)
(TTCAD25a: a digital macro on VAPWR, the board's analog supply fed from a variable PSU). An
on-chip LDO does not pay: published TT LDOs draw 19-74 uA quiescent (34-133 uW), more than this
chip. Preconditions for a future iteration:
1. Area: fit an analog 2x2 slot (~334 x 225 um): ~45-50k um^2 of cells (the final candidate is
   48.5k um^2 synthesised, but 8 channels did not route in a 2x2, F23).
2. Interface: level shifters on the 1.8 V pin signals, or an interface that fits the 6 analog
   pins (< 500 ohm, 4 mA each).
3. Flow: harden the core as a macro on VAPWR and place it in the analog template by hand
   (TT's flow builds one power domain); VAPWR is the chip's shared analog rail, set on the board.

## Budgets

Final candidate = branch `area4-final-3x2` (RTL a0966c0, 3x2 tiles) unless stated.

| metric | value | limit / target | status |
|---|---|---|---|
| cell area | TT top 48,518 um^2 synth (337 flops, 126 clock gates); the lossy core alone was not re-measured (62,200 um^2 on 2026-10-08, before F22) | lossy core 173,000 / 86,600 | PASS |
| TT design utilisation (3x2, D10) | 0.553 post-CTS (`cts_preview` 38058666284); pre-DFT all-in 3x2 signed off at 0.542 (38025684427) | 70% / 60% | PASS |
| setup slack, ss, 200 ns | +129.0 ns (pre-layout) | >= 0 / 60 | PASS |
| hold slack, ff | +0.187 ns pre-layout; +0.250 ns post-CTS (preview) | >= 0 | PASS |
| power, running | routed, real data, signed off: **34.8 uW** TT top (e13, F20); pre-DFT all-in 3x2 preview 34.7 uW (synthetic data) | 40 / 16 | WARN |
| power, idle | routed, real data: 9.5 uW (e13); final candidate preview 9.45 uW (synthetic data) | 10 / 2 | WARN |
| processing latency | 1,846 us (F1, F13) | 2,253 / 1,900 us (derived bound, C-LAT-1) | PASS |
| delivery latency | 13.4 ms | 40 / 20 ms | PASS |
| bits/sample, 160 held-out files | 2.087 | 2.2 / 2.1 | PASS |
| median SNR | 18.0 dB (10th pct 13.3) | 17 / 18 dB | PASS |

## Test status (final RTL, 2026-10-10)

| suite | result |
|---|---|
| model (pytest, incl. change guard) | 76 pass (2026-10-08; not re-run, the model is unchanged since) |
| lint | 0 errors |
| `test/core` (II = 1, 256-slot frames) | 40 pass, 0 fail; T-BW-3 new, not run yet |
| `test/lossy` (incl. power scenarios) | 8 pass |
| `test/rans` | 6 pass (2026-10-08) |
| TT top through the pins, 200 ns | 5/5 (config, slot selector, readback, overflow pin, lossy); replay 4 pass, 1 skip |
| gate level: lossy core, TT top netlist | pass, pass |
| latch pre-check (place-and-route SDC) | 0 latch D endpoints below margin (F24) |
| layout (`cts_preview`) | **fail: F27** (a clock gate without CLK after CTS) |
| gate level with SDF (T-GL-3), routed c8fd3b5 | pass at nom tt/ss/ff, max ss, min ff: bit-exact, 0 timing-check violations (F28) |

## Data

**Bandwidth, real data** (T-IF-1, 4 packets): packets 670, 650, 637, 653 bytes; 2.55 bits/sample
on this excerpt, SNR 17.6 dB; out FIFO peak 1; flush 27 bytes in 55 clocks.
Bytes per frame (count of frames): 1: 207, 2: 265, 3: 182, 4: 124, 5: 87, 6: 30, 7: 11, 8: 2,
9: 2, 26-40 (flush frames): 4.

**Bandwidth, worst case** (T-BW-1 LFSR, 2026-10-07, before the eager drain): up to 45 bytes/frame, typically 18-24; flush 30 bytes. Since the eager drain: up to 101 bytes/frame (C-BW-2).

**Host turnaround search** (history: T-BW-2, retired by D8; LFSR, clocks per byte -> no loss?): 33 no, 17 no, 9 yes,
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
