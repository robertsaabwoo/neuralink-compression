# Testing: what exists, what it showed, what to build

Companion to `docs/constraints.md` (requirements, IDs `C-*`), `docs/platform.md` (sources) and `docs/budgets.md` (pass/fail numbers).
Quick view of everything from a terminal: `python scripts/status.py`.

## 1. How to run

| command | what | time |
|---|---|---|
| `python scripts/status.py` | read-only summary: direction, repo state, last results, budgets, open items | 1 s |
| `python nlc.py algo` | model tests, compression on held-out files, `algo_eval.py` (rate, SNR, generalisation, spikes, max error) | ~10 min (`--quick` 2 min) |
| `python nlc.py sim` | model, lint, every RTL suite incl. `test/core` (`nlc_core` at the real interface) | ~45 min (`--quick`: T-IF only) |
| `python nlc.py synth` / `sta` / `gate_sim` / `power` | area / timing / gate level (lossy core + TT top) / power scenarios | 3 / 1 / 10 / 25 min |
| `python nlc.py layout` | routed design of the last GDS run (`scripts/fetch_gds.py`, needs `gh`): T-PWR-3 real-data power with the clock tree, T-FAN-1 buffer trees/slews (`reports/latest/layout_fanout.md`) | ~10 min |
| `python nlc.py all` | everything (T2), scored against `scripts/flow/budgets.json` | ~1.5 h |
| `python nlc.py report` | print `reports/latest/summary.md` | |
| `python nlc.py accept` | accept the current bitstream as the golden reference (T-CHG-7) | |
| `pytest` | golden-model unit tests incl. the change guard (host Python) | 30 s |
| `docker run --rm -v "$PWD:/work" -w /work/test/core nlc-flow make COCOTB_TEST_FILTER=t_if_1` | one cocotb suite or test (also `test/`, `test/lossy`, `test/rans` with `TOP=static/adaptive`) | 0.5-40 min |

`nlc.py` wraps `scripts/check.py` (steps `model lint rtl core synth area sta gl power layout compress algo`,
`--only`, `--quick`, `--native`, `--update-baseline`). `make` in a cocotb folder cannot take a
`|` in `COCOTB_TEST_FILTER` (the shell pipes it); use a prefix or a character class.

Data: real-data tests read the challenge WAVs from `data/raw` (743 files, flat, sorted-name
order; 300+ held out). The download URL is dead (404), so `scripts/fetch_data.py` takes a local
`data.zip` (README.md, Data). Without `data/raw` the tests fall back to synthetic data and
print one `NO REAL DATA` warning; CI always runs synthetic.

`check.py` runs inside the `nlc-flow` image (build once: `python docker/fetch_inputs.py && docker build -t nlc-flow docker`).
Results: `reports/latest/summary.md`, `reports/latest/metrics.json`, logs and netlists next to them.

### 1.1 Experiments on GitHub Actions (no local CPU)

| what | how | time |
|---|---|---|
| CTS preview of a design variant | push a branch: `.github/workflows/cts_preview.yaml` runs LibreLane up to post-CTS repair, then the layout step with wire parasitics estimated from placement; job summary + artifacts `cts_preview` (`python scripts/fetch_gds.py --preview --run <id>`) and `cts_preview_report` | ~5 min |
| Full sign-off of a variant | the same push runs TT's `gds` workflow (cancel it for throwaway experiments: `gh run cancel <id>`) | ~45-80 min |
| Nightly sweep | edit `sweeps/nightly.json` on branch `nightly` and push: `.github/workflows/nightly.yaml` runs every entry (design from `ref`, LibreLane config overrides, `noid` = patched image without CTS latency balancing, full or preview flow) through LibreLane + the layout step, plus long test/core regressions; the `aggregate` job summary / artifact `nightly-table` holds one table (`scripts/ci/nightly_aggregate.py`) | ~3 h for ~20 full runs |
| Compare reports | `python scripts/ci/compare_reports.py a=<report dir> b=<report dir>` | s |

CI power is on synthetic data (the dataset is not public): it ranks variants. Re-measure winners
locally on real data: put the routed design (`data/gds/<sha7>/` inside a nightly or gds artifact,
or `scripts/fetch_gds.py --run <gds run id>`) under `data/gds/`, write its name to
`data/gds/LATEST`, then `python scripts/check.py --only layout`.

**Routed gate-level failures:** a routed (post-CTS) netlist that is bit-exact in RTL but fails the
unit-delay gate-level simulation (bytes differ, X on `m_valid`) has so far always been a race of the
unit-delay model on skewed gated-clock trees, not a logic error (docs/results.md F21). TT's
gl_test uses the same model. Check STA hold first; the open fix is an SDF (real-delay)
gate-level simulation (T-GL-3).

## 2. Inventory of existing tests

### 2.1 Golden model (`model/tests`, pytest, 47 tests)
| file | covers |
|---|---|
| `test_bitstream.py` | MSB-first bit packing, unary/truncated unary, overflow/overread errors |
| `test_lossless.py` | mode 0: hand-computed vector, zigzag, Rice k selection, round trips (incl. escape), compression |
| `test_lossy.py` | mode 1: integer lifting perfect reconstruction, **streaming lifting == block transform**, production order covers every coefficient once, **FIFO peak 7 / lag 6 frames**, rANS round trip, escapes, n_flush, quality (SNR > 10 dB) and rate |
| `test_rans_tdm.py` | TDM rANS: adaptive tables, unused-channel flush, count cap, extreme tables, rate vs entropy, corrupt-packet detection, static tables |
| `test_spikes.py` | modes 2/3: high-pass, refractory, binned/SBP round trips, saturation |
| `test_packet_data.py` | packet header and seq wrap, wrong-mode rejection, WAV -> 10-bit code mapping, dataset loader, energy break-even |

### 2.2 RTL (cocotb 2.1, Icarus, bit-exact against the golden model)
| suite | DUT | tests | checks |
|---|---|---|---|
| `test/lossy` | `nlc_lossy` (wavelet, shared lifter, rANS, serialiser) | `adjacent_slots_short_frames` (8 channels back to back, 64-slot frames: below the D9 rate, a functional check on real data), `spread_slots_256` (8 of 256 slots, one slot per clock), `power_window` (1 packet at the operating point) | bytes == model, **then decoded** by the model; `overflow` = 0; prints bits/sample and SNR. Real challenge data (files 300+, not in ROM training) or synthetic if `data/raw` is absent |
| `test/rans` | `rans_tdm_static`, `rans_tdm_adaptive` (`test/rans/ref`, reference RTL, not in silicon) | static: reset table, loaded tables + reload, full-rate II=1, writes ignored while packet open; adaptive: random gaps + backpressure, full-rate II=1 | bytes == model, decoded |
| `test/` (TT top) | `tt_um_nlc_compressor` through the pins | `test_plumbing`: config registers, slot selector, config readback (T-IF-6), overflow pin (T-IF-7); `test_modes`: **lossy**; `test_abort`: abort token at the pins (`uio[7]` = 1 with `uio[6]` = 0) for disable mid-packet and a short frame, then a clean resume (T-ROB-2/4 at the top); `test_power`: T-PWR-3 scenarios | bytes == vectors from `scripts/gen_vectors.py` (32-slot frames, 2 clocks per slot) |
| `test/` with `ENCODER=replay` | TT top with `test/mock/nlc_encoder_replay.v` | same, minus the overflow pin (needs the real encoder) | harness self-test: pins, config, readback, slot selector, output capture with the golden bytes replayed |
| `test/core` | `nlc_core` (slot selector, encoder, config) at the real interface: 256 slots, one per clock (II = 1, D9), 200 ns | T-IF-1/2/3, T-BW-1/3, T-ROB-1..7, T-PWR-2 (T-ROB-1 needs `NLC_LONG=1`) | environment `test/env/nlc_env.py` (4.1): ADC mux, config port, host model (takes every byte, D8), scoreboard by seq, monitors for latency/bandwidth/coverage; one JSON per test in `$NLC_RESULTS` |

The TT-top suite runs at 200 ns (D2), one slot every 2 clocks, 32-slot frames (64 clocks):
the pin path is a test-access mode below the D9 rate (C-IF-9), fine for real and synthetic
data; lossy only (D1: the modes 0/2/3 tests and vectors were removed). `make GATES=local
NETLIST=reports/latest/top_netlist.v` runs it on our Yosys netlist (T-GL-2). `test/lossy` adds the
power scenarios of 4.4 (`test_power_op/n4/worst/floor`), `gl_dump.v` takes `+vcd_start=<ns>`.

### 2.3 Gate level, implementation (`scripts/check.py`)
| step | what |
|---|---|
| `lint` | Verilator `-Wall` on the lossy core (strict) and the TT top (errors only) |
| `synth` | Yosys -> sky130_fd_sc_hd (tt), flat netlists of `nlc_lossy` and the TT top, area, flop count, per-module area |
| `sta` | OpenSTA 3.1, setup at ss (100 C, 1.60 V) for the TT clock and 200 ns, hold at ff; ideal clock, no wires |
| `gl` | `test/lossy` `power_window` on the lossy netlist with sky130 cell models: at 200 ns (dumps VCD) and at the TT clock |
| `power` | VCD -> per-pin activity sampled per clock (`scripts/flow/vcd_activity.py`) -> OpenSTA `report_power` at 5 MHz; also with glitches and idle (clock only) |
| `compress` | golden model over 160 held-out challenge files (32-191): bits/sample, median SNR |

## 3. Results log

| date | run | result |
|---|---|---|
| 2026-10-06 | model | 47/47 pass |
| 2026-10-06 | `test/lossy` RTL (first version) | failed at byte 139: last-sample mirror used the stored odd register; fixed (`nlc_lift53`), then bit-exact |
| 2026-10-07 | `test/rans`, `test/lossy`, `test/` after port to cocotb 2.1 | pass (TT top: plumbing 2/2, lossy 1/1; lossless/binned/sbp fail: encoder stub, expected) |
| 2026-10-07 | `test/` `ENCODER=replay` | 7/7 |
| 2026-10-07 | gate-level lossy | **failed: 7,033 bytes instead of 670.** Yosys and Icarus disagreed on signed casts/function arguments; fixed with explicit bit slices; pre-mapping netlist and sky130 netlist now bit-exact |
| 2026-10-07 | power tooling | Debian OpenSTA 2.0.17 reported 0 internal power (even one clocked flop); replaced by OpenSTA 3.1 built from source. `read_vcd` only annotated 34 pins (needs cell-level VCD); replaced by `vcd_activity.py` (100% of 10,601 pins) |
| 2026-10-07 | full `check.py` (`reports/20261007-004244`) + compression rerun | see [results.md](results.md) |
| 2026-10-07 | `test/core` (42 cases, 25 min, `nlc_core` at 256 slots, 1 slot/clock, 200 ns) | 31 pass, 10 fail, 1 skip. All failures are findings F2-F4 ([results.md](results.md)) |
| 2026-10-07 | TT top at 200 ns, 1 slot / 2 clocks, 64-clock frames (T-IF-4); same on the Yosys TT-top netlist (T-GL-2) | pass; pass (277 s) |
| 2026-10-07 | `test/lossy` power scenarios in RTL; `op` at gate level (2 packets from frame 64) | pass; 628.6 uW (unchanged: still 98% flop clock pins) |
| 2026-10-08 | `test/core` on the clock-gated RTL (5a94b62), parallel runner (6 jobs, 10 min instead of ~25) | 34/46 pass, 0 unexpected; the 12 failures are the known D5-D7 requirement tests |
| 2026-10-08 | **intended format change, T-CHG-7 golden accepted:** eager FIFO drain, symbols coded frame by frame, channel by channel, burst in push order (`nlc.lossy.coding_order`) instead of j-major | packet sizes unchanged (synthetic 730/729, lfsr 5510/5445 bytes): only the byte order differs |
| 2026-10-08 | full check (model, lint, rtl, core, synth, sta, gl, power; 17.8 min with the parallel runner) on eager drain + gated TT top | all pass except T-ROB-5 (test deposited into renamed registers; fixed: it now finds every `nlc_greg`, 189 registers, 3/3 pass); new T-PWR-2 (`t_pwr_op`/`t_pwr_idle`, `nlc_core` gate level): 15.9 / 2.5 uW |
| 2026-10-08 | D5-D7 implemented (abort/skip/resume, abort token, short frame = abort); lint, rtl, core (10.7 min), replay | **all pass: test/core 48/48, KNOWN_FAIL empty**; lossy 8/8, TT top 4/4, replay 4/4. Gate level / STA not re-run for D5-D7 (functional check only, as asked); `gds` action on the 4x2 tiles is the next hardening check |
| 2026-10-08 | `layout` on the routed design of `a9c2329` (T-PWR-3, T-FAN-1); `power` re-run on D5-D7 RTL | routed netlist bit-exact on real data through the pins; 80.0 uW op / 51.4 uW idle (pre-layout same scenario 16.8 / 6.0), clock buffers 47.8 / 39.4 uW (F17); 3,283 / 366 slew pins at ss / tt, same as LibreLane (F18); T-PWR-1/2 on D5-D7: lossy 8.30 uW, core 12.5 / 1.07 uW (annotator change verified neutral: identical activity on the same VCD) |
| 2026-10-09 | power round on the routed design (CTS latency balancer, pin registers, E1/E3; nightly sweep of 21 LibreLane variants) | e13: 34.8 / 9.5 uW running / idle, routed, real data, signed off (F19-F21) |
| 2026-10-09/10 | area experiments B (shared lifter), C (looped divider, DIV_K = 5), G (flow: drive-1 cells, hold margins, CTS buffers), glue, D8 (valid-only output), S3 (latch rows) stacked; CDC + DFT on the TT top (a0966c0) | final RTL: TT-top synth 48,518 um^2, 337 flops; lint 0, `test/` 5/5, `test/core` 40 pass / 0 fail, `test/lossy` 8/8, final-netlist gate level (TT top + lossy) pass, STA ss +129.0 ns, ff hold +0.187 ns; only flow FAIL was `latency_processing_us` 1,846 against the old 1 ms (F1; C-LAT-1 restated to the derived 2,253 us bound, now PASS); T-BW-3 6/6 (F22-F27) |
| 2026-10-10 | 2x2 fit (gds runs on GitHub) | 8 channels do not route in 2x2 (F23); 3x2 and 4x2 signed off (gds, gl_test, precheck) for the stack and the pre-DFT all-in build |
| 2026-10-10 | final candidate `area4-final-3x2` (a0966c0 in 3x2): latch pre-check (place-and-route SDC), `ENCODER=replay`; `cts_preview` | latch D endpoints chased: 0; replay 4 pass, 1 skip (overflow pin needs the real encoder); preview util 0.553, hold +0.250 ns, layout gate-level **fails: OpenROAD CTS left a clock gate without CLK (F27, open)** |

Findings F1-F27, budgets and the measured data: [results.md](results.md).

## 4. Verification plan (testing phase)

Every test checks one or more constraints in `docs/constraints.md` (IDs `C-*`). Scope is set
by the decisions there: lossy mode only, 5 MHz with one slot per clock, packet-granular
overflow, Neuralink-derived power. This phase builds **tests, environment models and
measurement**. It does not build compressor RTL. The only RTL allowed is test peripherals
(the on-chip stimulus generator, section 4.2).

Status: `exists` = in the repo and passing, `extend` = exists but must change, `new`.

### 4.1 Test environment (simulation of the platform around the chip)

One reusable Python environment (`test/env/`, new), shared by every RTL and gate-level test,
so that each test only picks a scenario:

| component | models | knobs | status |
|---|---|---|---|
| **ADC mux driver** | the 256-slot stream at one slot per clock, `s_frame` on slot 0, never waits (C-IF-1..3) | `n_slots` (256; short frames for robustness only), data source per slot, frame-length faults (C-IF-8) | extend `test/harness.py` (`SLOT_CYCLES` -> 1, frame 256) |
| **Data sources** | challenge recordings (held-out files 300+), synthetic, generator patterns (4.2), edge patterns: all 0, all 1023, DC + step, full-scale square at Nyquist (largest wavelet details), single-channel spikes | per-channel source, seed | extend `model/nlc/data.py` |
| **Pin-path driver** | the slow real-data path through the pins with the `src/project.v` strobe protocol (C-IF-9: below the D9 rate, aborts allowed for worst-case data) | clocks per slot (>= 2), slots per frame | exists (harness) |
| **Host model** | takes a byte in every clock with `m_valid` (D8: no ack, no stall); drops a partial packet on the abort token. The turnaround/stall models of the `m_ack` protocol were retired with D8 | | exists |
| **Config driver** | register writes; legal sequences and illegal ones (write while enabled, non-ascending slots) | | exists, extend |
| **Scoreboard** | splits the byte stream into packets on `m_last`, checks header/seq, compares each packet with `LossyCodec` for its frames (by seq, so lost packets are allowed when the test expects them), decodes, computes SNR and bits/sample | expected-loss / expected-abort modes (T-ROB-2/4) | exists (`test/env/nlc_env.py`) |
| **Monitors** | per-sample timestamps (slot in, symbol into coder, packet's last byte out) for latency; bytes per frame for bandwidth; coverage counters (4.5) | | exists |
| **Reports** | each test writes a JSON of measured values (latency, bandwidth, coverage) that `scripts/flow/flow.py` collects into `reports/latest/metrics.json` and scores against budgets | | new |

System-model twin: the same chain without RTL (data source -> `LossyCodec` -> host model
as a byte-rate queue -> decoder) in pure Python (`model/nlc/system.py`, new). It runs hours of
data in seconds and finds worst-case packets for bandwidth and FIFO occupancy. Those packets
are then replayed in RTL (model-guided worst cases).

### 4.2 Stimulus peripheral (on-chip 256-slot generator, TT build only)

Purpose: the TT pins and the RP2040 cannot feed 4.94 MS/s for long (platform.md 4.4). The
peripheral produces the real interface on chip. It is test equipment, not part of the
compressor, and its area and power are reported separately.

| item | requirement |
|---|---|
| G-1 | Drives `nlc_core`'s ADC port in place of the pins: one slot per clock, 256 slots, `s_frame` on slot 0 |
| G-2 | Patterns: **ramp** (sample = f(slot, frame), trivially checkable), **LFSR** noise (worst case: escapes, maximum output rate), **constant** (minimum activity, for the power floor) |
| G-3 | Deterministic from reset/seed; a Python model (`model/nlc/stimgen.py`) reproduces the exact stream |
| G-4 | Selected via one config register (address and fields fixed when it is built; must not collide with `nlc_regs.vh`) |
| G-5 | Pin path still works when the generator is off |

Tests: T-GEN-1 (generator RTL == its model, sample for sample), T-GEN-2 (compressor output with
each pattern == `LossyCodec` on the model stream, through the TT top).

### 4.3 Test list

**Algorithm (golden model, pytest / scripts; fast, no RTL)**

| ID | test | pass | covers | status |
|---|---|---|---|---|
| T-ALG-1 | Model invariants: lifting perfect reconstruction, streaming == block, coefficient order, FIFO peak 7 / lag 6, rANS round trip, escapes | as today | C-FN-1 | exists (47 tests) |
| T-ALG-2 | Rate/quality regression over **all 743 files** (held-out set separate from ROM training): bits/sample, median and 10th-pct SNR, per-file table, worst 10 files listed | C-ALG-1..3 | C-ALG-1..3, C-BW-1 | extend (`compress` step runs 160 files) |
| T-ALG-3 | Table generalisation: rate on training files vs held-out files; table regeneration (`gen_lossy_rom.py`) is deterministic and equals the committed ROM | C-ALG-4; ROM byte-identical | C-ALG-4 | new |
| T-ALG-4 | Packet independence: decode packet k alone (no earlier packets) == decoding in sequence | exact | C-FN-3 | new |
| T-ALG-5 | Spike fidelity: threshold crossings (the model's detector, `model/nlc/spikes.py`) on original vs reconstruction: hit rate, false positives, timing jitter | report, then set a limit | C-ALG-5 | new |
| T-ALG-6 | Input edge cases in the model: all-0, all-1023, Nyquist square, DC steps, saturating data: round trip, escape counts, bytes per packet | decodes; max bytes recorded | C-BW-2 | new |
| T-ALG-7 | Error bound: max abs reconstruction error, overall and per subband, over the full dataset, recorded as a baseline (any increase = regression) | <= baseline | C-FN-2 | new |

**Algorithm change guard (run on every edit to `model/nlc/lossy.py`, `rans_tdm.py`, the ROM tables or the lossy RTL)**

Purpose: you will keep changing the compression algorithm (shifts, block length, levels,
tables, s_max, escape format, ...). These tests answer "does it still work?" with
**properties, not frozen bytes**. Any legal algorithm change passes them, and a broken one
fails. The tests read every parameter from `LossyConfig` and never hard-code today's values.

| ID | test | pass | covers | status |
|---|---|---|---|---|
| T-CHG-1 | **Round trip, all data**: encode -> decode for every data source (real, synthetic, edge patterns, LFSR) and several `LossyConfig` variants (block 32/64/128, levels 1-4, other shifts, s_max); `hypothesis` property test on random inputs | decodes; error within the bound the quantiser shifts allow; exact when all shifts = 0 | C-FN-2 | new |
| T-CHG-2 | **Hardware assumptions still hold** for the edited config: FIFO peak (`fifo_profile`) <= RTL depth 8, lag <= 1 frame; worst-case bytes per frame <= frame length at 1 byte/clock (C-IF-9, `scripts/proofs/output_bound.py`); coefficient/symbol widths fit the RTL fields (QW 13 bits, state 22 bits); table frequencies sum to M and fit the ROM width | all true, or a message naming the RTL parameter that must change | C-FN-1, C-IF-9 | new |
| T-CHG-3 | **Quality/rate gate**: T-ALG-2 on a fixed 32-file subset (fast), then the full held-out set in T2 | C-ALG-1/2 limits; WARN if > 2% worse than the baseline | C-ALG-1..3 | new (wraps the `compress` step) |
| T-CHG-4 | **Streaming == block reference**: the streaming production-order encoder (what the RTL does) == the block transform for the edited config | identical coefficients | C-FN-1 | exists in `test_lossy.py`, parametrise over configs |
| T-CHG-5 | **Model <-> RTL constant sync**: every `LossyConfig` field the RTL hard-codes (`nlc_lossy.sv` localparams, `nlc_rans.sv` parameters, `nlc_lossy_rom.v` contents) is parsed from the sources and compared with the model; ROM regenerated from `lossy_tables.json` equals the committed ROM | equal; on mismatch, list the fields | C-FN-1 | new (pattern exists: `regs.check_hw_constants`) |
| T-CHG-6 | **Model <-> RTL bit-exactness** after any edit: `test/lossy` + T-IF-1 regenerate expected bytes from the *current* model, so they follow the algorithm automatically | bit-exact | C-FN-1, C-FN-5 | exists, keep it model-driven |
| T-CHG-7 | **Format change detector**: a small committed golden file (bytes of 2 packets of fixed data + a hash of the tables). If the model's output changes, the test fails with "bitstream changed: if intended, run `python scripts/gen_vectors.py --accept`", so format changes are always deliberate and recorded in section 3 | unchanged, or accepted explicitly | C-FN-4 | new |
| T-CHG-8 | **Cost re-measure** (T2): after an accepted algorithm change, the flow reruns area, STA and power and compares with the baseline, so the silicon cost of the change is visible | WARN > 10% worse than baseline | C-AREA-*, C-PWR-* | exists (`check.py` baseline), document the use |

Workflow for an algorithm edit: `pytest -k chg` (T-CHG-1..5, 7; < 1 min) -> `check.py
--only rtl` (T-CHG-6) -> `check.py` (T-CHG-3 full, T-CHG-8). If T-CHG-2 or T-CHG-5 fails, the
edit needs a matching RTL change before T-CHG-6 can pass. That RTL change is design work and
happens later; the test only tells you which parameter is affected.

**Interface (RTL, `nlc_core` and TT top, cocotb)**

| ID | test | pass | covers | status |
|---|---|---|---|---|
| T-IF-1 | `nlc_core` at the real interface: 256 slots, one per clock, n_sel = 8, real data, >= 4 packets | bit-exact, 0 lost samples | C-IF-1..3, C-FN-1/2 | exists, pass |
| T-IF-2 | Slot selection sweep: n_sel 1..8; slots {0}, {255}, {0..7}, {248..255}, spread, random ascending sets (seeded) | bit-exact for each | C-IF-4 | exists, pass (11 slot sets) |
| T-IF-3 | Start-up: enable mid-frame -> first packet starts at the next `s_frame`, seq 0; enable -> disable -> enable == fresh run | bit-exact, seq 0 | C-IF-5/6 | exists, pass (F2 fixed by D7) |
| T-IF-4 | TT top through the pins at 5 MHz: config, pin path (C-IF-9, 1 slot / 2 clocks, 32-slot frames), host capturing every clock with `m_valid` (D8), pin synchronisers and registered outputs (C-IF-11) | bit-exact | C-IF-9..11 | exists, pass (`test_modes.test_lossy` at 200 ns) |
| T-IF-5 | TT top with the generator (T-GEN-2) at full rate | bit-exact | C-IF-1, G-* | new |
| T-IF-6 | Config readback on `uo_out` (cfg_en = 1, enable = 0, address strobe); a readback writes nothing | values match the writes | C-IF-12 | exists, pass (`test_plumbing.test_config_readback`) |
| T-IF-7 | Sticky overflow on `uio[4]`: 8-slot frames (far below the D9 rate) make the coder fall a frame behind; `enable` = 0 clears it | pin high, then cleared | C-IF-12, C-OVF-6 | exists, pass (`test_plumbing.test_overflow_pin`) |

**Overflow (decision D3) - retired by D8**

The output has no back-pressure since D8, so a host that stops taking bytes does not exist.
Aborts are covered by T-IF-3, T-ROB-2/4/7 and the overflow pin by T-IF-7.

| ID | test | was | status |
|---|---|---|---|
| T-OVF-1 | Host stops for a whole packet, then resumes | complete packets bit-exact, abort token, seq gap (D6) | **retired (D8)**; passed on the D6 RTL (2026-10-08) |
| T-OVF-2 | Stalls at the header, mid-payload, in the flush | packets after the resume delivered (D6) | **retired (D8)**; passed on the D6 RTL |
| T-OVF-3 | Byte drop + sticky flag, the "before" point | documented F3 | **retired (D8)** |

**Latency and bandwidth (measured by monitors, scored by the flow)**

| ID | test | pass | covers | status |
|---|---|---|---|---|
| T-LAT-1 | Timestamp every sample: slot -> coder input, slot -> last byte of its packet; report min/median/max | C-LAT-1/2 limits | C-LAT-1..3 | exists (T-IF-1 monitors) |
| T-BW-1 | Bytes per frame and per packet (histogram), flush burst length; real, LFSR and edge-pattern data (spread slots) plus model-guided worst packets (4.1) | report | C-BW-1/2 | exists, pass |
| T-BW-2 | Host turnaround sweep (clocks per byte) on worst-case data | was C-BW-3: 11, then 4 clocks/byte | C-BW-3 | **retired (D8)** |
| T-BW-3 | **Throughput guarantee at II = 1:** `nlc_core`, one slot per clock, 256-slot frames, worst-case data (LFSR, Nyquist square, full scale) on 8 adjacent channels at the end and at the start of the frame (the tightest placement of `scripts/proofs/output_bound.py`) | no abort token, 0 lost packets, bit-exact | C-IF-9, C-OVF-5 | exists, pass (`t_bw_3`, 6/6 on a0966c0: worst 117 bytes/frame vs the proof's 128-byte bound) |

**Robustness**

| ID | test | pass | covers | status |
|---|---|---|---|---|
| T-ROB-1 | Long run: >= 70 packets (seq wraps past 63) with the generator, RTL | bit-exact, seq wraps | C-FN-4 | exists (`NLC_LONG=1`), not run yet |
| T-ROB-2 | Reset and disable at random points (mid-frame, mid-packet, during flush; the host-stall point retired by D8); next run == fresh run | bit-exact, no hang | C-IF-6 | exists, pass (`test/core`; disable mid-packet also through the pins, RTL and gate level: `test_abort.test_abort_disable`) |
| T-ROB-3 | Config written while enabled (illegal), then disable/enable | no hang; correct afterwards | C-IF-7 | exists, pass |
| T-ROB-4 | Frame faults: early `s_frame` (3 variants), late, missing; strict against the frame rule D5 | bit-exact; a short frame aborts its packet (token, seq gap) | C-IF-8, C-OVF-1..4 | exists, pass (`test/core`; short frame also through the pins, RTL and gate level: `test_abort.test_abort_short_frame`) |
| T-ROB-5 | Power-up state: random contents deposited into every register without reset (Icarus, `t_rob_5`, several seeds); X-init is covered by every other Icarus/GL test, where the env fails on X/Z on `m_valid`/`m_abort` (and `m_data`/`m_last` with `m_valid`). No Verilator 2-state run exists | identical bytes for all seeds; no X on outputs after reset | C-FN-6 | exists, pass (random deposit, 234 regs, 3 seeds) |
| T-ROB-6 | Random regression: N seeded runs mixing data source, n_sel and slots (the random host was retired by D8; nightly) | 0 failures; failing seed reproducible | all C-FN, C-IF | exists, pass (4 seeds) |
| T-ROB-7 | `rst_n` mid-packet, during the flush (host-stall point retired by D8) | output empty right after reset; next run == run from power-up (D7) | C-IF-6 | exists, pass |

**Gate level, equivalence, implementation**

| ID | test | pass | covers | status |
|---|---|---|---|---|
| T-LINT-1 | Verilator lint: lossy core `-Wall`, TT top errors | 0 errors | C-RTL-1 | exists |
| T-GL-1 | Lossy core netlist (sky130 cells) runs `test/lossy` at 200 ns | bit-exact | C-FN-5, C-RTL-2 | exists |
| T-GL-2 | **TT top** netlist (like TT's `gl_test`) runs T-IF-4, T-IF-5 and the abort paths (`test_abort`) | bit-exact | C-FN-5, C-TIM-4 | exists, pass (Yosys netlist, `GATES=local`) |
| T-GL-3 | Post-layout gate-level with SDF from the TT GDS action | bit-exact | C-FN-5, C-TIM-1 | new (after first GDS run) |
| T-EQ-1 | RTL vs netlist equivalence (Yosys `equiv_*` or SymbiYosys) as a fast check next to T-GL-1 | proven equivalent | C-FN-5, C-RTL-2 | new |
| T-FV-1 | Formal properties on the plumbing (SymbiYosys): slot selector emits each configured slot exactly once per frame, in order | proven (bounded) | C-IF-2/4 | new |
| T-STA-1 | OpenSTA: setup at ss, 200 ns; hold at ff; report Fmax at ss | C-TIM-1/2 | C-TIM-1/2/5 | extend (TT clock -> 200 ns) |
| T-AREA-1 | Yosys area: lossy core, TT top, per module, flops; generator reported separately | C-AREA-1..3 | C-AREA-1..3 | extend (generator split) |
| T-AREA-2 | Area vs N_SEL sweep (1, 2, 4, 8): fixed cost and per-channel slope; state bits per channel | report | C-AREA-4 | exists (`area` step, N_SEL 2/4/8/16), not run yet |
| T-PWR-1 | Power matrix at 5 MHz, tt corner (4.4) | C-PWR-1..5 | C-PWR-* | extended: scenarios op/n4/worst/floor + idle; op measured |
| T-PWR-3 | **Post-layout power**: the routed netlist of the last GDS run (clock tree as CTS built it, repair and hold buffers) simulated through the TT pins on real data at the operating frame rate (128 slots x 2 clocks = 256 clocks/frame, 8 channels, `test/test_power.py`), bytes checked against the model; OpenSTA with the sign-off SDC (propagated clock) and extracted parasitics (nom SPEF). Clock-network nets use raw toggles. Same scenario on our pre-layout TT-top netlist for the difference. Power by cell class: clock buffers, clock gates, flops, repair buffers, hold buffers, logic | C-PWR-1/2 (`layout_power_uw`, `layout_power_idle_uw`) | C-PWR-1/2 | exists (`layout` step) |
| T-FAN-1 | **Buffer trees and slews per RTL signal** on the routed netlist: every data net with a real driver expanded through the buffers below it (true fanout, buffers, depth, buffer area, cell types), named by the RTL nets upstream of anonymous drivers; worst slew per tree at ss and tt with routed parasitics vs the sign-off max transition; clock tree summary (free-running vs gated buffers, nested gates). `reports/latest/layout_fanout.md` | report; slew violations WARN | C-PWR, C-AREA | exists (`layout` step) |
| T-SO-1 | TT GDS action: utilisation, setup/hold after CTS, precheck/DRC/LVS/antenna, LibreLane DisconnectedPins | C-TIM-2/3, C-AREA-3/5 | | exists (GitHub `gds` workflow; `cts_preview` for the first ~5 min of it) |
| T-INF-1 | CI: GitHub `test` (TT) and `ci` (model, RTL) workflows green | green | extended (`model.yml`: TT top + core T-IF); not pushed yet |
| T-INF-2 | Source-list consistency: `info.yaml`, `test/Makefile`, `LOSSY_SRC` list the same files | identical sets | C-RTL-4 | new |

### 4.4 Power and area measurement method

T-PWR-1 scenarios. Each is a gate-level run of the lossy netlist at 200 ns, then per-clock
activity, then OpenSTA `report_power` (method as in section 2.3):

| scenario | data | n_sel | window | limit |
|---|---|---|---|---|
| op | real held-out data | 8 | >= 2 packets, steady state (skip the first packet's start-up) | C-PWR-1 |
| op4 | real data | 4 | 2 packets | C-PWR-3 |
| worst | LFSR generator | 8 | 2 packets | C-PWR-5 |
| floor | constant generator | 8 | 1 packet | info |
| idle | `enable` = 0, clock running | | 1 packet length | C-PWR-2 |

Rules: the window must be long enough that power per packet changes by < 5% between packets
(checked and reported). Report a breakdown: flop clock pins, sequential internal,
combinational, ROM, leakage, plus glitch-inclusive totals (`op_with_glitches`). Report
energy per sample, power density, and the radio ratio (marked "assumes 10 nJ/bit, unsourced").
Corners: tt for the budget, ss/ff reported. T-PWR-3 repeats `op` and `idle` on the routed
netlist with SPEF parasitics and the clock tree (`python nlc.py layout`); that number is the
sign-off value for C-PWR-1. It runs the TT top through the pins, so it includes the pin glue
(compare with the pre-layout run of the same scenario, not with T-PWR-1/2).

Area: pre-layout Yosys cell area at tt is the working number (T-AREA-1/2). The GDS action's
utilisation and placement density are the sign-off (T-SO-1).

### 4.5 Functional coverage (collected by the monitors, reported per run)

Must be hit across the regression (T-ROB-6 plus the directed tests), otherwise the run WARNs:

- escapes in each context 0..3; consecutive escapes; an escape as the last symbol of a packet;
- every symbol value 0..63 in each context (or listed as unreachable);
- rANS hazard stall (`s_tready` low) and back-to-back same-channel symbols;
- coder output of 0, 1, 2, 3, 4 bytes in one word;
- packet flush overlapping new samples of the next packet;
- n_sel = 1..8; slot 0 and slot 255 selected; adjacent selected slots;
- seq wrap 63 -> 0; enable dropped at each phase (frame, block, packet, flush).

### 4.6 Regression tiers and CI

| tier | when | contents | time budget |
|---|---|---|---|
| T0 quick | before every commit | `pytest` (incl. T-CHG-1..5, 7), lint, `test/lossy`, `test/` plumbing + lossy (`check.py --only model,lint,rtl --quick`) | < 3 min |
| T1 CI | every push (GitHub `.github/workflows/model.yml` + TT `test.yaml`) | T0 + rANS suites + replay self-test + T-IF-* + T-INF-2 | < 15 min |
| T2 full | before a design change is accepted; nightly while iterating | `python scripts/check.py`: everything above + GL + STA + area + power matrix + compression on held-out files + random regression (T-ROB-6, 50 seeds) | < 1 h |
| T3 sign-off | per GDS run | TT `gds`, `gl_test`, precheck; then locally `python nlc.py layout` (T-PWR-3, T-FAN-1); T-GL-3 | TT action + ~10 min |

CI rules: A WARN never fails CI; a FAIL does. Every failing randomised test
prints its seed and a one-line command that reproduces it.

### 4.7 Order of work

1. **Environment** (4.1) and the **algorithm change guard** (T-CHG-*): extend the harness to 1 slot/clock and 256-slot frames, add the
   scoreboard, host model and monitors; system-model twin.
2. **Interface + robustness** tests that run on today's RTL: T-IF-1..4, T-ROB-1..5, T-FV-1,
   T-INF-2. They either pass or find bugs (record them in section 3).
3. **Measurement**: T-LAT-1, T-BW-1/2, power matrix, area sweep, T-ALG-2..7. This turns the
   "measure" entries of `docs/constraints.md` into numbers and fills in C-BW-3.
4. **Peripheral**: generator + model (4.2), T-GEN-1/2, T-IF-5, T-GL-2.
5. **Overflow tests** T-OVF-1..3 (done; retired by D8, replaced by T-BW-3 and T-IF-7).
6. Restore the TT template files, push, first GDS run -> T-SO-1, T-GL-3, post-layout power.

The testing phase is done when every constraint in `docs/constraints.md` has a test that
measures it, the T2 report shows a value for every budget, and the known failures (power,
overflow policy) are reproducible by a named test.

### 4.8 Traceability (constraint -> tests)

| constraints | tests |
|---|---|
| C-IF-1..3 | T-IF-1, T-IF-5, T-FV-1 |
| C-IF-4 | T-IF-2, T-FV-1 |
| C-IF-5..8 | T-IF-3, T-ROB-2..4 |
| C-IF-9 | T-BW-3, T-IF-4 |
| C-IF-10/11 | T-IF-4 |
| C-IF-12 | T-IF-6, T-IF-7 |
| C-FN-1/2 | T-ALG-1, T-CHG-1..7, T-IF-*, T-GL-*, T-ROB-6 |
| C-FN-3 | T-ALG-4, T-ROB-4 |
| C-FN-4 | T-ROB-1 |
| C-FN-5 | T-GL-1..3, T-EQ-1 |
| C-FN-6 | T-ROB-5 |
| C-OVF-1..4 | T-ROB-4, T-IF-3, T-ROB-2 |
| C-OVF-5 | T-BW-3 |
| C-OVF-6 | T-IF-7 |
| C-LAT-* | T-LAT-1 |
| C-BW-* | T-BW-1, T-BW-3, T-ALG-6 |
| C-TIM-* | T-STA-1, T-GL-2/3, T-SO-1 |
| C-AREA-* | T-AREA-1/2, T-SO-1 |
| C-PWR-* | T-PWR-1 (4.4), T-PWR-2, T-PWR-3, T-FAN-1 |
| C-ALG-* | T-ALG-2/3/5, T-CHG-3 |
| C-RTL-* | T-LINT-1, T-GL-1, T-EQ-1, T-INF-2 |
