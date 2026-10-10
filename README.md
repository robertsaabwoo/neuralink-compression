# neuralink-compression

Lossy neural-signal compression for a Tiny Tapeout chip (sky130). The chip runs a LeGall 5/3
integer wavelet, a quantiser and static rANS on 8 channels, picked from a 256-slot, 10-bit ADC
stream at 5 MHz. The goal is to measure what this mode costs in silicon: area, power and timing
after layout. Winning on compression ratio is not the goal. One-page overview:
[docs/SUMMARY.md](docs/SUMMARY.md).

## Why

A Neuralink-style implant reads 256 electrodes per ASIC through one time-multiplexed 10-bit ADC:
- one sample per slot;
- a new slot every ~200 ns;
- each channel comes round every 51 us.

The radio carries ~1 Mbit/s for 1024 channels. The implant must stay under 10 mW, radio
included ([docs/platform.md](docs/platform.md)). That leaves ~980 bit/s and ~10 uW per channel
for compression and radio together. Raw waveforms fit for only a few channels. That is how the
compression patent (US 12,369,863 B2) uses its lossy mode: 4-8 broadband channels, wavelet plus
entropy coding. This chip implements that mode for 8 configurable slots out of the 256.

## Block diagram

```
          256-slot TDM stream, 10 bit (core: 1 slot/clock; TT pins: 1 slot per 2 clocks)
               |
TT pins -> project.v   pin registers, strobe/ack edge detect, config bytes
               |
           nlc_core
            |- nlc_cfg       enable, n_sel (1-8), slot of each channel
            |- nlc_slot_sel  slot counter, picks the configured slots, frame rule (short frame = abort)
            |- nlc_encoder -> nlc_lossy
            |     5/3 lifting x 3 levels (one shared datapath, per-channel state rows)
            |  -> quantise (d1>>3, d2>>2, d3>>2, a3>>1, a3 delta-coded)
            |  -> per-channel burst buffer (up to 4 symbols per sample)
            |  -> rANS coder (22-bit state per channel, ROM tables, combinational divider)
            |  -> serialiser: header {mode, seq}, coded bytes, final states
            |- nlc_out_fifo  8 entries of {abort, last, byte}
               |
TT pins <- uo_out = byte, uio[6] = m_valid, uio[7] = m_last; host acks each byte on uio[4]
```

The design runs on one clock: 5 MHz, the ADC slot rate. The ADC is never stalled.

A packet covers 256 frames (13.1 ms) and decodes on its own.

Every register that does not need every clock sits behind a sky130 `dlclkp` clock gate, in two
levels.

Detail: [docs/architecture.md](docs/architecture.md).

## Results (signed-off design)

The design on this branch is commit `921e1e0` ("e13"). TT's flow signed it off in
[gds run 37865677796](https://github.com/robertsaabwoo/neuralink-compression/actions/runs/37865677796):
gds, gl_test and precheck are green. The `viewer` job fails only because GitHub Pages is not
enabled. All numbers: typical corner, 5 MHz, 8 channels.

| metric | value | budget (limit / target) | caveat |
|---|---|---|---|
| compression | 2.087 bits/sample, 18.0 dB median SNR (10th percentile 13.3 dB), 160 held-out recordings | 2.2 / 2.1 b/s; 17 / 18 dB | needs the challenge dataset (`scripts/fetch_data.py`) |
| power, routed, real data, running | **34.8 uW** (compression core share 23.8 uW, ~3.0 uW/ch) | 40 / 16 uW | see power notes |
| power, routed, idle (`enable` = 0) | **9.5 uW** (core share 0.80 uW) | 10 / 2 uW | see power notes |
| power, routed, input-port buffers off | 33.9 / 8.5 uW | | a flow variant of the same RTL, measured locally; not a TT sign-off run |
| area, synthesised | lossy core 60.1k um^2; TT top ~68k um^2 | | Yosys, sky130_fd_sc_hd |
| area, routed | 102.6k um^2 standard cells (11.7k of it repair/hold buffers), utilisation 0.69 of a 4x2 tile | | LibreLane |
| setup / hold, routed | +118.5 ns at 200 ns (ss) / +0.187 ns (ff) | >= 0 | |
| delivery latency | 13.1 ms | 40 / 20 ms | |
| processing latency | **1,846 us** | 1 / 0.41 ms | **FAIL**, see Known limitations |

**Power notes.**
- T-PWR-3 re-simulates the routed netlist of the gds run on real recordings, driven through the
  TT pins. OpenSTA `report_power` then runs on that activity, so the clock tree as built is
  included.
- The figure covers the whole TT design: pins, clock tree and core.
- The activity comes from a unit-delay simulation, not an SDF simulation.
- e13 was built with a **patched LibreLane image**. Its CTS runs `-no_insertion_delay`, which
  turns latency balancing off (F19). This is not TT's standard flow.
- The same RTL on TT's standard flow measures 51.5 / 20.8 uW (running / idle).
- The best TT-standard variant (E1 RTL) measures 40.7 / 17.8 uW, just over the 40 uW limit.
- The 51.5 and 40.7 figures are local re-measurements of layouts built by CI.
- History: [docs/results.md](docs/results.md) F11 and F17-F20.

**Power ladder** (running / idle):

| step | power | what changed |
|---|---|---|
| first RTL, pre-layout | 628.5 uW (lossy core) | 98% of it was flip-flop clock pins |
| clock gating, pre-layout | 9.5 uW (lossy core) | two-level `dlclkp` gating (F11) |
| `a9c2329`, routed | 80.0 / 51.4 uW | the clock tree is 48 / 39 uW of it (F17) |
| e13, routed | **34.8 / 9.5 uW** | small clock buffers, gated pin registers, no CTS latency balancing, selective pin loads, a core gate (F19, F20) |

**Area ladder** (only the e13 row is signed off):

| design | area | status |
|---|---|---|
| first RTL | 128k um^2 lossy core | historical |
| clock gating, eager FIFO drain, narrower buffers | 76.9k -> 62.2k -> 59.2k um^2 lossy core (F11, F13, F15) | historical |
| e13 (this branch) | lossy core 60.1k (abort logic added); TT top ~68k synthesised, 102.6k routed; 4x2 tiles | **signed off** |
| `area3-lift` (shared lifter) | 59.1k TT top, synthesised | WIP branch |
| `area4-stack` (shared lifter, looped divider, latch rows, valid-only output, 2x2 tiles) | 47.5k TT top, synthesised; post-CTS utilisation 0.78, hold +0.012 ns | **WIP**: gds and full regression pending |

## Verification matrix

Requirement IDs: [docs/constraints.md](docs/constraints.md). Test IDs: [docs/testing.md](docs/testing.md).
Status is for e13 unless the row says otherwise.

| requirement | test | level | data | status |
|---|---|---|---|---|
| bit-exact with the golden model (C-FN-1/2) | T-IF-1 (8 ch x 4 packets), `test/lossy` | RTL | real recordings (local), synthetic (CI) | pass |
| same, through the TT pins | T-IF-4; TT `test` and `gl_test` | RTL; gate level, unit delay | synthetic | pass ([gl_test](https://github.com/robertsaabwoo/neuralink-compression/actions/runs/37865677796)) |
| routed netlist == model | T-PWR-3 | routed gate level, unit delay | real, 2 packets | pass |
| any 1-8 channels in any slots (C-IF-4) | T-IF-2 | RTL | | pass |
| enable/disable, illegal config, reset (C-IF-6/7, D7) | T-IF-3, T-ROB-2/3/7 | RTL | | pass at `a9c2329` (48/48 `test/core`). e13 changed only pin and clock-gate logic; CI re-ran only T-IF on it |
| frame faults (C-IF-8, D5) | T-ROB-4 | RTL | | pass at `a9c2329` |
| host stall: abort, seq gap, recovery (C-OVF-1..4, D6) | T-OVF-1/2 | RTL | | pass at `a9c2329` |
| worst-case host rate (C-BW-3) | T-BW-2 | RTL | LFSR | pass at 4 clocks/byte (see Known limitations) |
| independent of power-up state (C-FN-6) | T-ROB-5 | RTL | random register contents, 3 seeds | pass at `a9c2329` |
| compression (C-ALG-1/2) | T-ALG-2 | model | 160 held-out files | pass |
| power (C-PWR-1/2) | T-PWR-3 | routed | real | under the limits, over the targets |
| timing, DRC/LVS, precheck (C-TIM, C-AREA-5) | T-SO-1 | TT flow | | pass |
| processing latency (C-LAT-1) | T-LAT-1 | RTL | real | **fail** (1,846 us) |
| real-delay gate level | T-GL-3 (SDF) | | | **not done** |
| scan / DFT | | | | **none** |

## Known limitations

- **Processing latency is 1,846 us, against a 1 ms target** (C-LAT-1, F1).
  - A sample is fully coded only when the last symbol that depends on it is coded.
  - Through the 3-level lifting and the delta-coded a3, that can be 36 frames later. Queue time
    alone is 307 us.
  - Which definition the 1 ms applies to is still open. Fewer wavelet levels would trade
    compression for latency.
- **Gate-level simulation is unit-delay only** (F21).
  - Some routed variants failed it while STA hold passed at all corners and RTL stayed bit-exact.
  - These failures are attributed to races of the unit-delay model on skewed gated-clock trees.
    That is not proven.
  - The open fix is an SDF simulation (T-GL-3).
- **Pin CDC and reset.**
  - Host pins are captured by a single flop, with no 2-flop synchroniser.
  - Raw `uio_in` levels drive clock-gate enables in `project.v`.
  - The release of `rst_n` is not synchronised, and some async clears come from logic
    (`rst_n && enable`).
  - Being fixed on branch `area4-stack-cdc`.
- **Host rate (e13).**
  - Measured worst case: 4 clocks/byte, on LFSR data (C-BW-3).
  - A max-plus bound with every symbol at its 4-byte worst case needs a host at 2 clocks/byte,
    which gives +30 clocks of slack at 256-clock frames.
  - At 64-clock frames (allowed on the pin path, C-IF-9) the bound does not close even at
    2 clocks/byte.
  - A slower host on adversarial data can lose a packet. The loss is detected (abort token, seq
    gap) and the next packet recovers (D6).
- **Throughput guarantee of the valid-only output** (`area4-stack`, D8; not on this branch).
  - With no host handshake, the same bound holds at the real 256-clock frame (+138 clocks of
    slack).
  - It is only +10 at 128-clock frames, and -118 at the 64-clock frames the TT pin tests use.
  - So short test frames can abort on worst-case data.
- **No DFT:** no scan, no test-enable on the clock gates, and config is write-only.
- **Power is above the 16 / 2 uW goals** (34.8 / 9.5 uW). It needs the patched CTS step to stay
  under the 40 uW limit (see power notes).
- **Storage does not scale to 256 channels.** State lives in flops and latches. 256 channels need
  single-port SRAM rows, as in Neuralink's spike-detection patent.

## Reproduce

Everything except `pytest` runs in a Docker image with Icarus, cocotb 2.1, Yosys, OpenSTA 3.1 and
the sky130 models:

```
python docker/fetch_inputs.py && docker build -t nlc-flow docker   # once
python scripts/fetch_data.py   # challenge recordings (archived copy); without them the tests use synthetic data
pytest                         # golden model only, no Docker (30 s)
python nlc.py sim              # model + all RTL suites incl. test/core (~45 min; --quick: interface tests only)
python nlc.py algo             # compression on held-out files
python nlc.py synth            # area; also: sta, gate_sim, power
python nlc.py layout           # routed design of the last gds run (needs gh): T-PWR-3, T-FAN-1
python nlc.py all              # everything, scored against scripts/flow/budgets.json (~1.5 h)
python nlc.py report           # print reports/latest/summary.md
```

- `nlc.py` wraps `scripts/check.py` (options `--only <steps>` and `--quick`).
- Results go to `reports/latest/summary.md` and `metrics.json`.
- Reports are not committed to the repository.

## Docs

| doc | what |
|---|---|
| [docs/SUMMARY.md](docs/SUMMARY.md) | one page: problem, approach, numbers, story |
| [docs/platform.md](docs/platform.md) | Neuralink's architecture and Tiny Tapeout's limits, with sources |
| [docs/constraints.md](docs/constraints.md) | requirements (C-*) and decisions D1-D7 |
| [docs/architecture.md](docs/architecture.md) | how the RTL is built, what it costs, where to change what |
| [docs/testing.md](docs/testing.md) | how to run, test inventory, verification plan (T-*) |
| [docs/results.md](docs/results.md) | findings F1-F21, budgets, measured data |
| [docs/budgets.md](docs/budgets.md) | where each pass/fail number comes from |
| [docs/info.md](docs/info.md) | TT datasheet page: pins and how to drive them |

## Layout

```
model/nlc/     golden model: the bit-exact spec (format in the module docstrings)
model/tests/   pytest, incl. the algorithm change guard
src/           RTL: project.v (TT pins) -> nlc_core -> slot_sel / nlc_lossy / out_fifo
src/robs_rANS/ reference pipelined rANS coders (tested by test/rans; not in the chip)
test/env/      shared test environment (ADC stream, host, scoreboard, monitors)
test/core/     nlc_core at the real 256-slot interface
test/lossy/    lossy core + power scenarios;  test/rans/: rANS coder;  test/: TT top
scripts/flow/  check flow and budgets
```

## Attribution

Inspired by Neuralink's patents (US 2021/0012909 A1, US 12,369,863 B2) and by the public
Neuralink compression challenge, whose recordings are the test data. This is an independent
learning project using standard public techniques. It is **not affiliated with or endorsed by
Neuralink**.
