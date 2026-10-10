# Constraints: what the lossy design must meet

The requirement list for the design phase. Each constraint has an ID, a value, where the value
comes from, and the test(s) in `docs/testing.md` section 4 that check it. Constraints say
**what is observable**, never how the RTL achieves it: mechanisms belong to the design phase.

Sources: `docs/platform.md` (Neuralink, TT), `docs/budgets.md` (numeric pass/fail, mirrored in
`scripts/flow/budgets.json`). "Derived" = computed here from sourced numbers, arithmetic shown.
"Set by us" = no external source; chosen so the test has a line to pass.

## 0. Scope decisions (2026-10-06)

| # | decision | consequence for testing |
|---|---|---|
| D1 | **Silicon = lossy mode (1) only.** Modes 0/2/3 stay in the golden model, not in RTL | The RTL has no registers or tests for modes 0/2/3 (removed 2026-10-08; CTRL's mode bits are ignored); model tests for them stay |
| D2 | **Clock = ADC slot rate, 5 MHz (200 ns), one slot per clock** | STA, gate-level, power and the TT top are all checked at 200 ns; the 20 ns template default is not a target |
| D3 | **Output overflow loses whole packets** (no backpressure to the ADC is possible; mechanism: D6) | constraints C-OVF-*: a lost packet must be detectable from the pins and recovery is automatic |
| D4 | **Power budget is Neuralink-derived**: ~10 uW/channel for compression *and* radio | compressor share: target 2 uW/ch, limit 5 uW/ch (C-PWR-1) |
| D5 | **Frames are delimited by `s_frame`.** A short frame is a fault (the ADC sequencer cannot produce one when healthy): the packet holding it is aborted like D6 (abort token, seq gap) and output resumes at the next packet. The short frame still counts as one frame, so later packets stay aligned. Slots after the last selected one are ignored (long frame, missing `s_frame`). Revised 2026-10-08 from padding (missing channels repeating their previous sample), which needed buffering of up to 8 next-frame samples | frames as driven: `model/nlc/adc.py`; C-IF-8 |
| D6 | **Blocked output = abort and flush.** When a byte cannot be written (output FIFO full), the packet in flight is aborted: abort token to the host, every FIFO and the coder cleared, ADC samples ignored but frames still counted; output resumes with the next packet that starts after the host reads again. No packet is stored whole (a packet is up to 5.5 KB; the design holds ~1 KB) | C-OVF-*; replaces D3's "drop whole packets" |
| D7 | **Reset.** `rst_n`: everything cleared at once, output empty; the host resets with the chip. `enable` = 0: the same, and a packet the host has partly received is ended with the abort token | C-IF-6/7 |

## 1. Interface (environment the chip lives in)

| ID | constraint | value | source | tests |
|---|---|---|---|---|
| C-IF-1 | Input is a 256-slot multiplexed stream, one 10-bit sample per slot, slot 0 flagged by `s_frame` | 256 slots, 10 bit | patent [1] | T-IF-1, T-IF-2 |
| C-IF-2 | No backpressure on the input: every slot is consumed on time, in every state (packet flush, output stalled, overflow) | 0 lost/duplicated samples | patent [1] | T-IF-1, T-OVF-* |
| C-IF-3 | Slot rate = clock rate; per-channel rate = clock / 256 | 5 MHz -> 19.53 kHz (target 16-20 kHz) | [1], [4]; derived | T-IF-1 |
| C-IF-4 | Selected channels: `n_sel` 1..8, slots strictly ascending, any slot 0..255 including 0 and 255 and adjacent slots | 1..8 | patent [2] (4-8 lossy) | T-IF-2 |
| C-IF-5 | The first packet after `enable` starts on a frame boundary and has seq 0 | | `nlc_slot_sel` contract | T-IF-3 |
| C-IF-6 | `enable` = 0 clears all state; a packet the host has partly received ends with the abort token (D7); the next run is bit-identical to a run from reset | | D7 | T-IF-3, T-ROB-2 |
| C-IF-7 | Config writes happen only while `enable` = 0. A write while enabled must not hang the chip: after the next disable/enable, output is correct | | contract; set by us | T-ROB-3 |
| C-IF-8 | Frame faults (early, late or missing `s_frame`) never hang the chip and never shift the channels: a packet holding a short frame is aborted, every other packet equals the model on the frames as driven (D5) | | D5 | T-ROB-4 |
| C-IF-9 | Pin path (TT, real data from the host): strobe protocol of `src/project.v`, at most one slot every 2 clocks, frames at least **64 clocks** long | derived: lossy flush needs ~Q_W + 3 + 3 x N_SEL x SB / 2 = 10 + 3 + 36 = 49 cycles per frame (`nlc_lossy.sv` header) | T-IF-4 |
| C-IF-10 | Output: bytes on `uo_out`, `m_valid`/`m_last` on `uio[6]/uio[7]`, host takes a byte with an `m_ack` rising edge | | `src/project.v` | T-IF-4, T-BW-2 |

## 2. Functional correctness

| ID | constraint | value | tests |
|---|---|---|---|
| C-FN-1 | Every delivered packet is bit-exact with `LossyCodec` (`model/nlc/lossy.py`) for the same frames | 0 byte differences | T-ALG-1, T-RTL-*, T-GL-* |
| C-FN-2 | Every delivered packet decodes with the model decoder to the model's reconstruction | exact | T-RTL-* |
| C-FN-3 | Packets are independent: packet k+1 decodes without packet k | | T-ALG-4, T-OVF-2 |
| C-FN-4 | Header = mode 1, seq increments per packet modulo 64 | | T-ROB-1 |
| C-FN-5 | Netlist == RTL: gate-level output identical to RTL output for the same input (catches synthesis/simulator disagreement, e.g. the signedness bug) | | T-GL-1, T-GL-2, T-EQ-1 |
| C-FN-6 | Output is independent of power-up register contents (the per-channel rows have no reset) | identical bytes for any initial state | T-ROB-5 |

## 3. Output overflow (decision D3)

The host can stop taking bytes. The ADC cannot be stopped, so data will be lost. These rules
describe what the host sees. How the RTL does it is a design-phase item.

| ID | constraint | tests |
|---|---|---|
| C-OVF-1 | Loss is at **packet granularity**: every packet the host receives as complete is bit-exact (C-FN-1). Any packet that lost a byte is either not delivered or can be identified as damaged **from the pins alone** (there is no status read path today) | T-OVF-1 |
| C-OVF-2 | Seq numbers keep counting through lost packets, so the gap shows which packets are missing | T-OVF-1 |
| C-OVF-3 | Recovery needs no reset or reconfiguration: the first packet that starts after the host resumes is delivered intact | T-OVF-2 |
| C-OVF-4 | The input side is unaffected: no sample of a later packet is lost or shifted (C-IF-2) | T-OVF-2 |
| C-OVF-5 | Without host stalls, overflow never happens for any input data, including worst-case data, as long as the host meets C-BW-3 | T-BW-2 |

**Abort token (D6, D7).** At the `nlc_core` boundary a new output `m_abort` qualifies a
transfer: `m_valid` = `m_abort` = 1 means "discard the bytes of the current packet"; it travels
through the output FIFO in order with the bytes. On the TT pins (no free uio pin) the suggested
encoding is `m_last` = 1 while `m_valid` = 0 (implemented in `src/project.v`). Tests
T-OVF-*, T-IF-3, T-ROB-2/3 read `m_abort` when the port exists.

## 4. Latency

The challenge's "< 1 ms" [3] and the patent's 10-40 ms packet windows [2] conflict unless they
measure different things (platform.md section 3). Both are tracked:

| ID | constraint | limit | target | expected (derived) | tests |
|---|---|---|---|---|---|
| C-LAT-1 | **Processing latency**: sample slot -> its symbol enters the rANS coder | 1 ms [3] | 8 frames = 410 us | queue only: 6-frame FIFO lag + 13-stage pipeline = ~310 us (measured 307 us). **Measured including the transform look-ahead (36 frames): 2.0 ms, 1,846 us since the eager drain** (F1, F13). Open: which definition the 1 ms applies to | T-LAT-1 |
| C-LAT-2 | **Delivery latency**: sample slot -> last byte of its packet accepted by the host (packet decodable) | 40 ms [2] | 20 ms | 256 + 6 frames = 13.4 ms + drain | T-LAT-1 |
| C-LAT-3 | Packet window | 10-40 ms [2] | | 256 frames x 51.2 us = 13.1 ms | T-LAT-1 |

## 5. Output bandwidth

| ID | constraint | value | source | tests |
|---|---|---|---|---|
| C-BW-1 | Typical output rate on real data | ~326 kbit/s (2.087 b/sample x 8 ch x 19.53 kHz) | derived | T-BW-1 |
| C-BW-2 | Worst-case output rate over any frame and any packet (LFSR noise, full-scale square waves, escapes on every symbol), and the worst burst (packet flush) | **measured**: LFSR 21.4 bits/sample = 3.36 Mbit/s, up to **101 bytes/frame** since the eager FIFO drain (was 45: bursts are now coded in the frame they are pushed), flush 28 bytes (real data: 325 kbit/s, peak 30 bytes/frame) | T-BW-1, 2026-10-08 | T-BW-1 |
| C-BW-3 | Host requirement, published in the README/datasheet: the slowest host turnaround (clocks per byte) at which worst-case data gives no overflow | **measured: 4 clocks/byte (0.8 us at 5 MHz)** on LFSR data, 2 packets; 5 loses data. Was 11 before the eager FIFO drain; accepted by the user 2026-10-08: the real consumer is on-chip (~1 byte/clock), and 4 is about the TT pin protocol's own limit (2 clocks after each m_ack edge). Not a worst-case guarantee: with every symbol at 4 bytes the bound needs 2 clocks/byte (results.md F5) | T-BW-2, 2026-10-08 | T-BW-2 |
| C-BW-4 | Radio budget context (informational): 1 Mbps for 1024 channels = ~980 bit/s/ch; lossy is ~40 kbit/s/ch, i.e. only a few broadband channels fit | derived [3] | T-ALG-2 report |

## 6. Timing (sky130_fd_sc_hd, pre-layout then TT sign-off)

| ID | constraint | value | tests |
|---|---|---|---|
| C-TIM-1 | Setup at the slow corner (ss, 100 C, 1.60 V), clock 200 ns | slack >= 0 (limit), >= 60 ns (target, 30% kept for wires/skew pre-layout) | T-STA-1 |
| C-TIM-2 | Hold at the fast corner (ff, -40 C, 1.95 V) | >= 0 after TT's CTS/resizer (pre-layout: report only) | T-STA-1, T-SO-1 |
| C-TIM-3 | TT config: `src/config.json` `CLOCK_PERIOD` = 200, `info.yaml` `clock_hz` = 5000000 | to apply when the template files are restored | T-SO-1 |
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
