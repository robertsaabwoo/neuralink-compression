# neuralink-compression

Lossy neural-data compression for a Tiny Tapeout chip (sky130): a LeGall 5/3 integer wavelet,
quantisation and static rANS on 8 channels picked from a 256-slot, 10-bit ADC stream at 5 MHz.
The goal is to measure what this costs in silicon (area, power, timing), not to win on
compression ratio.

## Status

**Signed off for a Tiny Tapeout submission (digital, 1.8 V, 3x2 tiles):** gds, precheck and
gate-level test green (run 38060355759); the routed netlist is bit-exact with real delays at 5
corners. One page with every caveat: [docs/summary.md](docs/summary.md).

| | result | |
|---|---|---|
| compression | 2.09 bits/sample (4.8:1), 18.0 dB median SNR | pass |
| power (routed, real data) | in-scope block (compression core + clock root): 26.7 uW running / 2.8 uW idle. The TT pin interface (test access only, not in scope) adds 9.8 / 6.4 uW: whole TT chip 40.3 / 10.2 uW. Breakdown and scope: [docs/summary.md](docs/summary.md). Patched clock-tree step; TT's standard image: 49 / 17 uW for the chip. Was 629 uW before clock gating | pass (40 / 10 uW budget) |
| area | 48,500 um^2 of cells (from 128,000); 3x2 tiles, 56% utilisation | pass; 2x2 does not route at 8 channels |
| timing | +127 ns setup, +0.20 ns hold at 200 ns (routed) | pass |
| throughput | one ADC slot per clock; no input data can make it drop a packet at the real 256-clock frame (proof, +138 clocks of slack; RTL test T-BW-3) | pass |
| tests | model 80, core 46/46, TT top 7/7, gate level, SDF 5 corners, bring-up diagnosis 26/26 faults | pass |

Open items (detail in [docs/results.md](docs/results.md)): output not rate-capped (worst-case
data exceeds the raw rate); rANS tables fixed in ROM; fidelity reported as SNR, not
spike-detection agreement; test-access debug modes on a separate branch (router crash, being
signed off).

## Run it

All checks run in a Docker image (Icarus, cocotb 2.1, Yosys, OpenSTA, sky130 models):

```
python docker/fetch_inputs.py && docker build -t nlc-flow docker   # once
python nlc.py sim        # model + RTL tests (~45 min, --quick for the interface tests only)
python nlc.py algo       # compression quality on the challenge data
python nlc.py synth      # area;  also: sta, gate_sim, power
python nlc.py all        # everything, scored against the budgets (~1.5 h)
python nlc.py report     # last summary
python scripts/status.py # where things stand (1 s)
pytest                   # golden model only, no Docker (30 s)
```

Results land in `reports/latest/summary.md`.

### Data

Real-data results use the Neuralink compression-challenge recordings (1 h of non-human
primate motor cortex, 743 mono 16-bit WAV files, ~19.5 kHz, one electrode per file, 143 MB
`data.zip`). The published URL (`https://content.neuralink.com/compression-challenge/data.zip`)
returns 404 since 2026-10-06 and the repo does not redistribute the data, so get a copy of
`data.zip` from the challenge (or anyone who has it) and run
`python scripts/fetch_data.py path/to/data.zip`. Expected layout: `data/raw/<uuid>.wav`, all
743 files flat in one folder; files are used in sorted-name order (the first 32 train the rANS ROM,
300+ are held out for the RTL tests). Without `data/raw` every "real" test and the vectors run
on synthetic data instead and print a `NO REAL DATA` warning; CI is synthetic only.

## Docs

| doc | what |
|---|---|
| [docs/summary.md](docs/summary.md) | **start here**: one page, results with their caveats, verification, limits, next steps |
| [docs/demo.md](docs/demo.md) | an 8-minute live demo, each step timed |
| [docs/platform.md](docs/platform.md) | the environment the chip lives in: Neuralink's architecture, Tiny Tapeout limits |
| [docs/architecture.md](docs/architecture.md) | how the RTL is built, what it costs, where to change what |
| [docs/constraints.md](docs/constraints.md) | requirements (C-*) and decisions D1-D10 |
| [docs/testing.md](docs/testing.md) | how to run, test inventory, verification plan (T-*) |
| [docs/results.md](docs/results.md) | findings, budgets, measured data |
| [docs/budgets.md](docs/budgets.md) | where each pass/fail number comes from |
| [docs/bringup.md](docs/bringup.md) | first silicon: board capture -> diagnose.py -> faulty block / cell; validation |

## Layout

```
model/nlc/     golden model: the bit-exact spec (format in the module docstrings)
model/tests/   pytest, incl. the algorithm change guard
src/           RTL: project.v (TT pins) -> nlc_core -> slot_sel / nlc_lossy (valid-only output)
test/env/      shared test environment (ADC stream, host, scoreboard, monitors)
test/core/     nlc_core at the real 256-slot interface
test/lossy/    lossy core + power scenarios;  test/rans/: rANS coder;  test/: TT top
scripts/flow/  check flow and budgets
```

## Attribution

Inspired by Neuralink's patents (US 2021/0012909 A1, US 12,369,863 B2) and the public Neuralink
compression challenge, whose recordings are the test data. An independent learning project using
standard public techniques; **not affiliated with or endorsed by Neuralink**.
