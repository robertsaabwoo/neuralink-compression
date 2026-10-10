# Budgets and expected values

What "good" means for this design (requirement IDs and the tests that check them: `docs/constraints.md`), where each number comes from, and what the
design measures today. The machine-readable copy (limits, targets, baseline)
is `scripts/flow/budgets.json`; `python scripts/check.py` measures everything and
scores it. PASS = within target, WARN = past target or >10% worse than the
baseline, FAIL = past the limit.

## Operating point

8 channels selected from a 256-slot ADC mux at 19.53 kHz per slot, so the
clock is the slot rate: **5 MHz (200 ns)**. Library: sky130_fd_sc_hd. Power
at the typical corner (25 C, 1.80 V), setup at the slow corner (100 C, 1.60 V),
hold at the fast corner (-40 C, 1.95 V).

## Budgets

| metric | limit (FAIL) | target (PASS) | why |
|---|---|---|---|
| lint errors | 0 | 0 | TT's GDS flow runs a Verilator lint; errors stop the build |
| failed tests | 0 | 0 | model, lossy RTL, rANS RTL, TT top (implemented modes), lossy gate level |
| lossy core cell area | 173,000 um^2 | 86,600 um^2 | limit: fits TT **8x2** (16 tiles x ~18,000 um^2 x 60% density); target: **4x2** |
| lossy area per channel | 0.03 mm^2 | 0.01 mm^2 | published compression ASICs: 0.004-0.03 mm^2/ch at 130-180 nm [3] |
| whole TT design utilisation | 70% | 60% | TT's default `PL_TARGET_DENSITY_PCT` is 60 (template `src/config.json`) |
| setup slack, slow corner, TT clock | 0 ns | 60 ns | TT constrains `CLOCK_PERIOD` from `src/config.json`; set to **200 ns** (decision D2 in `docs/constraints.md`; template default 20 ns); 30% kept for wires and clock skew, since this is pre-layout |
| setup slack, slow corner, 200 ns | 0 ns | 60 ns | the real operating clock |
| hold slack, fast corner | 0 ns | 0 ns | ideal clock; TT's CTS/resizer fixes real hold |
| lossy core power | **40 uW** (5 uW/ch) | **16 uW** (2 uW/ch) | Neuralink-derived (decision D4): < 10 mW for 1024 channels including radio = ~9.8 uW/ch for compression *and* radio; the compressor gets at most half. Published compression ASICs: 0.9-15 uW/ch [3] |
| idle power (`enable` = 0) | 10 uW | 2 uW | set by us (C-PWR-2); measured by the `idle` power scenario, not yet scored by `flow.py` |
| power density | 40 mW/cm^2 | 10 mW/cm^2 | implant guideline ~40 mW/cm^2 (~2 C tissue heating, AAMI) [2]; area = cell area / 60% |
| compressor / radio energy | 0.10 | 0.01 | energy per sample vs the radio energy the compression saves; **informational**: 10 nJ/bit is unsourced (`docs/platform.md` section 1) |
| bits per sample | 2.20 | 2.10 | 160 held-out challenge files |
| median SNR | 17 dB | 18 dB | 160 held-out challenge files |

## Expected values (baseline, 2026-10-07)

Historical: the baseline the flow compares against (`budgets.json`), measured before clock
gating. Current values for the signed-off design (e13: 60.1k um^2 lossy core, 34.8 / 9.5 uW
routed) are in [results.md](results.md) "Budgets (e13)" and the README.

| metric | value | status |
|---|---|---|
| lint errors | 0 (46 `-Wall` warnings in the rest of the TT design, none fatal) | PASS |
| tests | model 47/47, lossy RTL 3/3, rANS 6/6, TT top 3/7 (4 pending: modes 0/2/3 not implemented), gate-level lossy pass | PASS |
| lossy cell area | 76,900 um^2 (2,127 flops + 159 clock gates; was 128,000) | PASS |
| area per channel | 0.016 mm^2 | WARN |
| TT design utilisation | 48% of 8x2 (139,400 um^2) | PASS |
| critical path (slow corner) | 61.9 ns: slack **-41.9 ns at 20 ns** | **FAIL** |
| setup slack at 200 ns | +138 ns | PASS |
| hold slack | +0.24 ns | PASS |
| lossy core power | 9.5 uW op, 0.77 uW idle at 5 MHz (was 628 uW; clock gating 2026-10-08) | PASS |
| of which flop clock pins (idle, no data) | 618 uW (98%) | |
| of which data activity | ~11 uW (glitches add ~0.3 uW) | |
| leakage | 0.04 uW | |
| energy per sample | 4.0 nJ | |
| compressor / radio energy | 0.051 | WARN |
| power density | 295 mW/cm^2 | **FAIL** |
| bits per sample | 2.087 | PASS |
| median SNR | 18.0 dB (10th percentile 13.3) | PASS |

### What the failures mean

- **Fixed 2026-10-08 (clock gating, docs/results.md F11); kept for the record:**
  **Power is clock power.** Every one of the 2,681 flip-flops is clocked every
  cycle (~0.23 uW each at 5 MHz), but each channel's ~220 bits of state change
  once per 256-cycle frame and the rANS pipeline is idle ~97% of the time. Clock
  gating (sky130 `dlclkp` integrated clock gates per channel row, and on the
  coder pipeline when idle) attacks 98% of the power; data activity is ~11 uW.
- **Timing at 50 MHz.** The front end does a whole sample in one cycle (state
  read, 3 lifting levels, quantiser, delta, FIFO write: ~60 cells deep). That is
  fine at the 5 MHz operating clock. For TT, either set `CLOCK_PERIOD` in
  `src/config.json` to the real clock (e.g. 100-200 ns) or pipeline the front end.

## Caveats

- Pre-layout: no wire capacitance, ideal clock (no clock-tree buffers). The
  routed design will be slower and use more power; TT's GDS action (LibreLane)
  is the sign-off for area and timing.
- Power activity is from a gate-level simulation of one packet of real
  challenge data at the operating point. The budgeted figure counts functional
  transitions (sampled per clock); `op_with_glitches` in the report includes the
  glitches of the unit-delay simulation.
- Local libraries come from the ciel sky130 release, which may differ slightly
  from the PDK version LibreLane pins.

## Sources

1. E. Musk & Neuralink, "An integrated brain-machine interface platform with
   thousands of channels", J. Med. Internet Res. 21(10), 2019: 256-channel ASIC,
   5.2 uW per analog pixel, ~6 mW per ASIC. https://www.jmir.org/2019/10/e16194/
2. A. Marblestone et al., "Physical Principles for Scalable Neural Recording",
   2013: AAMI-derived ~40 mW/cm^2 (~2 C), ~1-2 C limit for brain implants.
   https://arxiv.org/pdf/1306.5709
3. Review of on-chip neural data compression, Frontiers in Neuroscience 2024
   (doi 10.3389/fnins.2024.1432750): compressed sensing 0.95 uW/ch, 0.008 mm^2/ch
   (180 nm); lossless reduction 6.4 uW/ch, 0.004 mm^2/ch (130 nm); Huffman
   15.35 uW/ch, 0.098 mm^2/ch (180 nm); 22 nm codecs 0.3-4.4 uW/ch.
   https://www.frontiersin.org/journals/neuroscience/articles/10.3389/fnins.2024.1432750/pdf
