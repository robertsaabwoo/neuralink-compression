# neuralink-compression

Lossy neural-data compression for a Tiny Tapeout chip (sky130): a LeGall 5/3 integer wavelet,
quantisation and static rANS on 8 channels picked from a 256-slot, 10-bit ADC stream at 5 MHz.
The goal is to measure what this costs in silicon (area, power, timing), not to win on
compression ratio.

## Status

**Testing phase.** The RTL works and matches the Python model bit for bit. Now we are building
the test environment and measuring the design against Neuralink-derived requirements. Design
fixes come after that.

| | result | |
|---|---|---|
| compression | 2.09 bits/sample, 18.0 dB median SNR | pass |
| area | 128,000 um^2, 48% of an 8x2 TT design | over the 4x2 target |
| timing | +138 ns slack at 200 ns (slow corner) | pass |
| power | 629 uW, 98% of it flip-flop clock pins | **far over the 40 uW budget** |
| tests | 31 of 42 interface/robustness cases pass | 10 fail on purpose: open design issues |

Open design issues found by the tests (detail in [docs/results.md](docs/results.md)):
- processing latency is 2.0 ms against a 1 ms target (wavelet look-ahead);
- disabling mid-packet, a host stall, or one short ADC frame corrupts the output stream;
- a worst-case host must read a byte at least every 11 clocks (2.2 us).

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

## Docs

| doc | what |
|---|---|
| [docs/platform.md](docs/platform.md) | the environment the chip lives in: Neuralink's architecture, Tiny Tapeout limits |
| [docs/architecture.md](docs/architecture.md) | how the RTL is built, what it costs, where to change what |
| [docs/constraints.md](docs/constraints.md) | requirements (C-*) and decisions D1-D7 |
| [docs/testing.md](docs/testing.md) | how to run, test inventory, verification plan (T-*) |
| [docs/results.md](docs/results.md) | findings, budgets, measured data |
| [docs/budgets.md](docs/budgets.md) | where each pass/fail number comes from |

## Layout

```
model/nlc/     golden model: the bit-exact spec (format in the module docstrings)
model/tests/   pytest, incl. the algorithm change guard
src/           RTL: project.v (TT pins) -> nlc_core -> slot_sel / nlc_lossy / out_fifo
test/env/      shared test environment (ADC stream, host, scoreboard, monitors)
test/core/     nlc_core at the real 256-slot interface
test/lossy/    lossy core + power scenarios;  test/rans/: rANS coder;  test/: TT top
scripts/flow/  check flow and budgets
```

## Attribution

Inspired by Neuralink's patents (US 2021/0012909 A1, US 12,369,863 B2) and the public Neuralink
compression challenge, whose recordings are the test data. An independent learning project using
standard public techniques; **not affiliated with or endorsed by Neuralink**.
