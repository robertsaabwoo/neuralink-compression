# neuralink-compression

Lossy neural-data compression for a Tiny Tapeout chip (sky130): a LeGall 5/3 integer wavelet,
quantisation and static rANS on 8 channels picked from a 256-slot, 10-bit ADC stream at 5 MHz.
The goal is to measure what this costs in silicon (area, power, timing), not to win on
compression ratio.

## Status

**Hardening for a Tiny Tapeout submission (digital, 1.8 V, 3x2 tiles).** The RTL matches the
Python model bit for bit. Power and area are within the limits of the Neuralink-derived budgets; the open
item is an OpenROAD clock-tree bug on the final RTL (below).

| | result | |
|---|---|---|
| compression | 2.09 bits/sample, 18.0 dB median SNR | pass |
| power | 34.8 uW running, 9.5 uW idle: routed, real data, signed off on the pre-area-round RTL (e13); the 3x2 build before the test-access pins: 34.7 / 9.4 uW in a post-CTS preview on synthetic data. Was 629 uW before clock gating | under the 40 uW limit, over the 16 uW target |
| area | 48,500 um^2 of cells (from 128,000); 3x2 tiles, ~55% utilisation | pass; 2x2 does not route at 8 channels |
| timing | +129 ns setup slack at 200 ns (slow corner) | pass |
| throughput | one ADC slot per clock; no input data can make it drop a packet at the real 256-clock frame (proof, +138 clocks of slack; RTL test T-BW-3: worst-case data, 6/6 pass) | pass |
| tests | model, RTL, core and gate-level suites pass; signed-off GDS for the 3x2 build without the test-access pins | final RTL: placement density 64 avoids the CTS bug (F27); sign-off run pending |

Open items (detail in [docs/results.md](docs/results.md)):
- OpenROAD CTS left one clock gate without its clock pin at placement density 60 (F27); density
  64 avoids it (all 126 gates connected), the root cause in OpenROAD is not known;
- processing latency is 1.85 ms, set by the wavelet look-ahead (F1). It is not observable at the
  pins (packets decode whole, 13.4 ms); the challenge's "< 1 ms" is read as real-time
  throughput, which the chip guarantees (C-LAT-1, C-IF-9).

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
| [docs/platform.md](docs/platform.md) | the environment the chip lives in: Neuralink's architecture, Tiny Tapeout limits |
| [docs/architecture.md](docs/architecture.md) | how the RTL is built, what it costs, where to change what |
| [docs/constraints.md](docs/constraints.md) | requirements (C-*) and decisions D1-D10 |
| [docs/testing.md](docs/testing.md) | how to run, test inventory, verification plan (T-*) |
| [docs/results.md](docs/results.md) | findings, budgets, measured data |
| [docs/budgets.md](docs/budgets.md) | where each pass/fail number comes from |

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
