# Constraints: what the lossy design must meet

The requirement list for the design phase. Each constraint has an ID, a value, where the value
comes from, and the test(s) in `docs/testing.md` section 4 that check it. Constraints say
**what is observable**, never how the RTL achieves it: mechanisms belong to the design phase.

Sources: `docs/platform.md` (Neuralink, TT), `docs/budgets.md` (numeric pass/fail, mirrored in
`scripts/flow/budgets.json`). "Derived" = computed here from sourced numbers, arithmetic shown.
"Set by us" = no external source; chosen so the test has a line to pass.

## 0. Scope decisions (2026-10-06 to 2026-10-10)

| # | decision | consequence for testing |
|---|---|---|
| D1 | **Silicon = lossy mode (1) only.** Modes 0/2/3 stay in the golden model, not in RTL | The RTL has no registers or tests for modes 0/2/3 (removed 2026-10-08; CTRL's mode bits are ignored); model tests for them stay |
| D2 | **Clock = ADC slot rate, 5 MHz (200 ns), one slot per clock** | STA, gate-level, power and the TT top are all checked at 200 ns; the 20 ns template default is not a target |
| D3 | **Output overflow loses whole packets** (no backpressure to the ADC is possible; mechanism: D6) | constraints C-OVF-*: a lost packet must be detectable from the pins and recovery is automatic |
| D4 | **Power budget is Neuralink-derived**: ~10 uW/channel for compression *and* radio | compressor share: target 2 uW/ch, limit 5 uW/ch (C-PWR-1) |
| D5 | **Frames are delimited by `s_frame`.** A short frame is a fault (the ADC sequencer cannot produce one when healthy): the packet holding it is aborted like D6 (abort token, seq gap) and output resumes at the next packet. The short frame still counts as one frame, so later packets stay aligned. Slots after the last selected one are ignored (long frame, missing `s_frame`). Revised 2026-10-08 from padding (missing channels repeating their previous sample), which needed buffering of up to 8 next-frame samples | frames as driven: `model/nlc/adc.py`; C-IF-8 |
| D6 | **Blocked output = abort and flush.** When a byte cannot be written (output FIFO full), the packet in flight is aborted: abort token to the host, every FIFO and the coder cleared, ADC samples ignored but frames still counted; output resumes with the next packet that starts after the host reads again. No packet is stored whole (a packet is up to 5.5 KB; the design holds ~1 KB). **Amended by D8:** the output can no longer be blocked; the same abort path remains as a safety net when the coder falls more than a frame behind (only possible below the spec frame rate, D9) and for D5/D7 | C-OVF-*; replaces D3's "drop whole packets" |
| D7 | **Reset.** `rst_n`: everything cleared at once, output empty; the host resets with the chip. `enable` = 0: the same, and a packet the host has partly received is ended with the abort token | C-IF-6/7 |
| D8 | **Valid-only streaming output, no ready (2026-10-09).** The output FIFO and the `m_ack` handshake are removed: one byte per clock while `m_valid`, and the consumer takes every byte, up to 1 byte/clock. Rationale: in patent [1] the compression engine's packets go to on-chip merge circuitry and a serializer (platform.md 2.2); we take that path as never blocked, so a host that stalls the output does not exist | Retired: C-BW-3 (host turnaround), the blocked-output part of C-OVF (C-OVF-3/5 as written), T-BW-2, T-OVF-1/2/3, the `host_stall` cases of T-ROB-2/7 and T-ROB-6's random host. The TT pins capture every clock (C-IF-10) |
| D9 | **Throughput: the system is II = 1 (2026-10-10).** The core takes one ADC slot every clock in every state; a frame is 256 slots = 256 clocks (C-IF-1/3). Every consumer keeps up with its producer (the coder with the issuer, the merge/serializer with the output); nothing is slowed down to the frame rate. The no-abort guarantee is stated at this rate (C-IF-9) | C-IF-9, C-OVF-5, T-BW-3. Slower test feeds (the TT pin path) are below spec: aborts must be clean there, delivery is not guaranteed |
| D10 | **Silicon target (2026-10-10): digital TT submission at 1.8 V, 8 channels, 3x2 tiles** (4x2 acceptable). The 2x2 tile does not route at 8 channels (results.md F23); reducing to 4 channels to fit 2x2 was rejected. The analog-slot / low-voltage route is dropped | C-AREA-3 (utilisation) is judged on the 3x2 die |
| D11 | **DFT debug modes (2026-10-10).** One config register `DBG` (0x08), written only while `enable` = 0 (a write while enabled is ignored): mode 0 = normal, 1 = raw bypass (the input path pins -> synchronisers -> slot selector, without the codec), 2 = clock-gate enable observation (16 groups of 8 of the 127 gates). No pins are added (all 24 are used): the modes reuse `uo_out`, `m_valid`, `m_last`, `uio[4]`. Mode 0 must stay bit-exact, and the debug logic must be static in it (idle power) | C-IF-13/14, T-IF-8/9; every existing T-IF/T-ROB/T-GL test runs in mode 0 unchanged |

## 1. Interface (environment the chip lives in)

| ID | constraint | value | source | tests |
|---|---|---|---|---|
| C-IF-1 | Input is a 256-slot multiplexed stream, one 10-bit sample per slot, slot 0 flagged by `s_frame` | 256 slots, 10 bit | patent [1] | T-IF-1, T-IF-2 |
| C-IF-2 | No backpressure on the input: every slot is consumed on time, in every state (packet flush, abort) | 0 lost/duplicated samples | patent [1] | T-IF-1, T-BW-3, T-ROB-4 |
| C-IF-3 | Slot rate = clock rate; per-channel rate = clock / 256 | 5 MHz -> 19.53 kHz (target 16-20 kHz) | [1], [4]; derived | T-IF-1 |
| C-IF-4 | Selected channels: `n_sel` 1..8, slots strictly ascending, any slot 0..255 including 0 and 255 and adjacent slots | 1..8 | patent [2] (4-8 lossy) | T-IF-2 |
| C-IF-5 | The first packet after `enable` starts on a frame boundary and has seq 0 | | `nlc_slot_sel` contract | T-IF-3 |
| C-IF-6 | `enable` = 0 clears all state; a packet the host has partly received ends with the abort token (D7); the next run is bit-identical to a run from reset | | D7 | T-IF-3, T-ROB-2 |
| C-IF-7 | Config writes happen only while `enable` = 0. A write while enabled must not hang the chip: after the next disable/enable, output is correct | | contract; set by us | T-ROB-3 |
| C-IF-8 | Frame faults (early, late or missing `s_frame`) never hang the chip and never shift the channels: a packet holding a short frame is aborted, every other packet equals the model on the frames as driven (D5) | | D5 | T-ROB-4 |
| C-IF-9 | **Throughput (D9, II = 1):** at one slot per clock and 256-clock frames, no packet is aborted for **any** input data and any slot set (incl. 8 adjacent slots). Below that rate (shorter frames) the chip must stay clean (abort token, resume at the next packet, no hang, C-IF-8 rules), but delivery is not guaranteed for worst-case data. The TT pin path (strobe protocol of `src/project.v`, one slot per 2 clocks at most; the TT tests use 32-slot frames = 64 clocks) is such a test-access mode | derived, `scripts/proofs/output_bound.py` (D8 model: no FIFO, 1 byte/clock; every symbol 4 bytes = 2 escape + 2 renormalisation, 8 adjacent channels: worst case 8 x 4 x 4 = 128 bytes/frame). Minimum slack per frame length: 256 clocks **+138**, 192 +74, 128 +10 (inside the model's +-2 clocks/stage granularity: not claimed), 96 -54, 64 -118. Single-cycle and looped divider give the same numbers: the limit is the 1 byte/clock output | T-BW-3 (new), T-IF-4 |
| C-IF-10 | Output (D8): bytes on `uo_out`, `m_valid`/`m_last` on `uio[6]/uio[7]`, one byte in every clock with `m_valid` = 1; the host captures every clock (no ack). `m_last` = 1 with `m_valid` = 0 is the abort token. All outputs come from flops (one output register stage) | | `src/project.v` | T-IF-4 |
| C-IF-11 | Pin clock-domain crossing: `rst_n` asserts asynchronously and releases synchronously (2-flop reset synchroniser, the only reset inside the chip); `s_strobe` and `cfg_en` pass 2-flop synchronisers; no raw pin reaches a clock-gate enable. Host timing: sample and `s_frame` set up no later than `s_strobe` and held 2 clocks | recovery/removal checked by STA from the synchroniser | `src/project.v` | T-IF-4, T-STA-1 |
| C-IF-12 | Test access (DFT): `uio[4]` = sticky overflow (the coder fell a frame behind; cleared by `enable` = 0 or reset); config readback on `uo_out` while `cfg_en` = 1, `CTRL.enable` = 0 and an address byte was strobed (no write happens) | | `src/project.v` | T-IF-6, T-IF-7 |
| C-IF-13 | Raw bypass (D11, `DBG[1:0]` = 1): while enabled, the output carries the selected channels' samples instead of packets: per frame, per channel in slot order, `{6'b0, s[9:8]}` then `s[7:0]`, `m_last` on the frame's last byte; no header, no abort token. The encoder is held cleared (sees `enable` = 0, no clock edge). Needs one free clock after each selected sample: always true on the TT pin path (one slot per 2 clocks); at one slot per clock (D9) no two selected slots may be adjacent (nor the frame's last and the next frame's first); otherwise the due low byte is replaced by the next sample's high byte and `uio[4]` (overflow) is set. `DBG` reads back | 2 bytes per sample, 16 per frame at n_sel = 8 (of 256 clocks at D9, 64 on the pin path) | set by us | T-IF-8 |
| C-IF-14 | Clock-gate observation (D11, `DBG[1:0]` = 2): `m_valid` = 1 every clock (enabled or not), `m_last` = 0; `uo_out` bit b = the enable of gate (group `DBG[7:4]`, b) at the clock edge that loaded `uo_out` (map: `scripts/dft/icg_map.py`, docs/architecture.md). Config readback still takes `uo_out` (`m_valid` = 0 then). Observes the enable logic, not the gated clock: a gate whose cell or clock net is broken (results.md F27) still shows its enable, and a child gate's enable can read 1 while its parent is closed | 127 gates, 16 groups x 8 bits | set by us | T-IF-9 |

## 2. Functional correctness

| ID | constraint | value | tests |
|---|---|---|---|
| C-FN-1 | Every delivered packet is bit-exact with `LossyCodec` (`model/nlc/lossy.py`) for the same frames | 0 byte differences | T-ALG-1, T-RTL-*, T-GL-* |
| C-FN-2 | Every delivered packet decodes with the model decoder to the model's reconstruction | exact | T-RTL-* |
| C-FN-3 | Packets are independent: packet k+1 decodes without packet k | | T-ALG-4, T-ROB-4 |
| C-FN-4 | Header = mode 1, seq increments per packet modulo 64 | | T-ROB-1 |
| C-FN-5 | Netlist == RTL: gate-level output identical to RTL output for the same input (catches synthesis/simulator disagreement, e.g. the signedness bug) | | T-GL-1, T-GL-2, T-EQ-1 |
| C-FN-6 | Output is independent of power-up register contents (the per-channel rows have no reset) | identical bytes for any initial state | T-ROB-5 |

## 3. Lost packets (decisions D3, D6, D8)

Since D8 the host cannot stop the output, so a blocked output no longer exists. Packets are
still aborted on a short frame (D5), on `enable` falling (D7), and, as a safety net, when the
coder falls more than a frame behind (only possible below the D9 rate). These rules describe
what the host sees then.

| ID | constraint | tests |
|---|---|---|
| C-OVF-1 | Loss is at **packet granularity**: every packet the host receives as complete is bit-exact (C-FN-1). A packet that lost a byte ends with the abort token, so it is identified as damaged **from the pins alone** | T-ROB-4, T-IF-3, T-ROB-2 |
| C-OVF-2 | Seq numbers keep counting through lost packets, so the gap shows which packets are missing | T-ROB-4 |
| C-OVF-3 | Recovery needs no reset or reconfiguration: output resumes intact at the next packet boundary. (As written before D8, "after the host resumes": retired with the host stall) | T-ROB-4 |
| C-OVF-4 | The input side is unaffected: no sample of a later packet is lost or shifted (C-IF-2) | T-ROB-4 |
| C-OVF-5 | At the D9 rate (one slot per clock, 256-clock frames) the coder never falls a frame behind, for any input data including worst-case data (C-IF-9). Before D8 this was "as long as the host meets C-BW-3" (retired) | T-BW-3 (new); proof `scripts/proofs/output_bound.py` |
| C-OVF-6 | A coder overflow is visible on the pins: sticky `overflow` on `uio[4]` (C-IF-12) | T-IF-7 |

**Abort token (D5, D7).** `nlc_core` output `m_abort` (one clock, `m_valid` = 0) means "discard
the bytes of the current packet". On the TT pins: `m_last` = 1 while `m_valid` = 0. Tests
T-IF-3, T-ROB-2/3/4/7 read it.

## 4. Latency

The challenge's "< 1 ms" [3] does not say what it measures (platform.md section 3). We read it as
throughput: the chip keeps up with the input in real time, which C-IF-9 / D9 guarantee. End-to-end
latency is C-LAT-2: a sample is usable only once its packet is decodable (10-40 ms windows [2]).

| ID | constraint | limit | target | expected (derived) | tests |
|---|---|---|---|---|---|
| C-LAT-1 | **Coding look-ahead** (informational, decided 2026-10-10): sample slot -> the last coder symbol that depends on it. Fixed by the transform (5/3 DWT, 3 levels, 64-frame block, delta-coded a3: up to 36 frames = 1.84 ms) plus queue lag; not observable at the pins (a sample is usable only when its packet is decodable, C-LAT-2). The flow keeps it as a regression guard on the derived bound, not as the challenge's 1 ms | 44 frames = 2,253 us (36 look-ahead + 8 queue, derived) | 1,900 us | measured 1,846 us (look-ahead 1,843 us; queue alone 307 us, F1) | T-LAT-1 |
| C-LAT-2 | **Delivery latency**: sample slot -> last byte of its packet accepted by the host (packet decodable) | 40 ms [2] | 20 ms | 256 + 6 frames = 13.4 ms + drain | T-LAT-1 |
| C-LAT-3 | Packet window | 10-40 ms [2] | | 256 frames x 51.2 us = 13.1 ms | T-LAT-1 |

## 5. Output bandwidth

| ID | constraint | value | source | tests |
|---|---|---|---|---|
| C-BW-1 | Typical output rate on real data | ~326 kbit/s (2.087 b/sample x 8 ch x 19.53 kHz) | derived | T-BW-1 |
| C-BW-2 | Worst-case output rate over any frame and any packet (LFSR noise, full-scale square waves, escapes on every symbol), and the worst burst (packet flush) | **measured**: LFSR 21.4 bits/sample = 3.36 Mbit/s, up to **101 bytes/frame** since the eager FIFO drain (was 45: bursts are now coded in the frame they are pushed), flush 28 bytes (real data: 325 kbit/s, peak 30 bytes/frame) | T-BW-1, 2026-10-08 | T-BW-1 |
| C-BW-3 | **Retired by D8 (2026-10-09).** Was: the slowest host turnaround (clocks per byte) at which worst-case data gives no overflow; measured 4 clocks/byte after the eager FIFO drain (11 before). Now: the consumer takes every byte at up to 1 byte/clock, and C-IF-9 / C-OVF-5 carry the guarantee | T-BW-2, 2026-10-08 (history) | - |
| C-BW-4 | Radio budget context (informational): 1 Mbps for 1024 channels = ~980 bit/s/ch; lossy is ~40 kbit/s/ch, i.e. only a few broadband channels fit | derived [3] | T-ALG-2 report |

## 6. Timing (sky130_fd_sc_hd, pre-layout then TT sign-off)

| ID | constraint | value | tests |
|---|---|---|---|
| C-TIM-1 | Setup at the slow corner (ss, 100 C, 1.60 V), clock 200 ns | slack >= 0 (limit), >= 60 ns (target, 30% kept for wires/skew pre-layout) | T-STA-1 |
| C-TIM-2 | Hold at the fast corner (ff, -40 C, 1.95 V) | >= 0 after TT's CTS/resizer (pre-layout: report only) | T-STA-1, T-SO-1 |
| C-TIM-3 | TT config: `src/config.json` `CLOCK_PERIOD` = 200, `info.yaml` `clock_hz` = 5000000 | applied | T-SO-1 |
| C-TIM-4 | TT's `gl_test` (unit-delay gate-level sim of the TT top) passes at the test clock | | T-GL-2 |
| C-TIM-5 | Reported, no limit: maximum clock at ss (keeps the 50 MHz option visible) | info | T-STA-1 |

## 7. Area and storage

| ID | constraint | limit | target | tests |
|---|---|---|---|---|
| C-AREA-1 | Lossy core cell area | 173,000 um^2 (fits 8x2) | 86,600 um^2 (4x2) | T-AREA-1 |
| C-AREA-2 | Area per channel (N_SEL = 8) | 0.03 mm^2 | 0.01 mm^2 | T-AREA-1 |
| C-AREA-3 | Whole TT design utilisation (incl. stimulus generator) | 70% | 60% | T-AREA-1, T-SO-1 |
| C-AREA-4 | Per-channel cost, measured as the slope of area vs N_SEL (1, 2, 4, 8) | report; per-channel state bits compared with Neuralink's 226 b/ch | T-AREA-2 |
| C-AREA-5 | TT precheck, DRC, LVS, antenna clean | 0 violations | T-SO-1 |

## 8. Power and energy (decision D4)

Neuralink: < 10 mW for 1024 channels including radio [3], so ~9.8 uW per channel for
compression and radio together. The compressor gets a share of that budget. Radio energy per
bit has no source (platform.md section 1), so ratios that depend on it are informational.

| ID | constraint | limit | target | tests |
|---|---|---|---|---|
| C-PWR-1 | Lossy core power at 5 MHz, 8 channels, real data, typical corner | **40 uW** (5 uW/ch) | **16 uW** (2 uW/ch) | T-PWR-1 |
| C-PWR-2 | Idle power (`enable` = 0, clock running) | 10 uW | 2 uW | T-PWR-1 |
| C-PWR-3 | Power scales with selected channels: power(n_sel=4) clearly below power(n_sel=8) | report the slope | T-PWR-1 |
| C-PWR-4 | Power density (area = cell area / 60%) | 40 mW/cm^2 [AAMI] | 10 mW/cm^2 | T-PWR-1 |
| C-PWR-5 | Worst-case data power (LFSR stimulus) | report; must stay within C-PWR-1 limit | | T-PWR-1 |
| C-PWR-6 | Compressor energy / radio energy saved (assumes 10 nJ/bit, **unsourced**) | info only until sourced | | T-PWR-1 |

## 9. Compression quality (algorithm)

| ID | constraint | limit | target | tests |
|---|---|---|---|---|
| C-ALG-1 | Bits per sample, held-out files | 2.20 | 2.10 | T-ALG-2 |
| C-ALG-2 | Median per-channel SNR, held-out files | 17 dB | 18 dB | T-ALG-2 |
| C-ALG-3 | 10th-percentile SNR (the worst channels) | report; baseline 13.3 dB | | T-ALG-2 |
| C-ALG-4 | Static ROM tables generalise: rate on held-out files vs on training files | <= 3% worse (set by us) | | T-ALG-3 |
| C-ALG-5 | Spike fidelity: threshold crossings on the reconstruction vs the original | report first, then set a limit (no external source) | | T-ALG-5 |

## 10. Coding rules for synthesised RTL (from measured failures)

| ID | rule | why | tests |
|---|---|---|---|
| C-RTL-1 | Verilator lint: 0 errors; `-Wall` clean on the lossy core | TT's flow lints; errors stop the GDS build | T-LINT-1 |
| C-RTL-2 | No signed casts, `>>>` or signed compares in synthesised code; write sign handling as explicit bit slices | Yosys and Icarus disagreed (7,033 vs 670 bytes) | T-GL-1, T-EQ-1 |
| C-RTL-3 | Control registers behind a clock gate reset asynchronously; storage without reset uses `nlc_greg`. A synchronous reset synthesised through logic can stay X in gate-level simulation (X-pessimism on reconvergent logic, F16), and it would also hold the gate open | output FIFO pointers stayed X at gate level, 2026-10-08 | T-GL-2, T-PWR-2 |
| C-RTL-4 | All synthesised files listed in `info.yaml`, `test/Makefile` and `LOSSY_SRC` in `scripts/flow/flow.py` | the three lists drift | T-INF-2 |

## Sources

Numbered as in `docs/platform.md`: [1] US 2021/0012909 A1, [2] US 12,369,863 B2, [3] Neuralink
compression challenge README (archive), [4] Musk & Neuralink JMIR 2019. AAMI: `docs/budgets.md` [2].
