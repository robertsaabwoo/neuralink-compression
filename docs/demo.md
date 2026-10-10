# Demo script (about 8 minutes, on a laptop)

Before: `python docker/fetch_inputs.py && docker build -t nlc-flow docker` done; the challenge
recordings in `data/raw/` (not public, see the README's Data section); the browser open on
[summary.md](summary.md) and on the sign-off run
(https://github.com/robertsaabwoo/neuralink-compression/actions/runs/38060355759).
Commands run from the repo root (Git Bash on Windows: prefix the `docker` lines with
`MSYS_NO_PATHCONV=1`).

| # | show | command | time | point |
|---|---|---|---|---|
| 1 | golden model | `python -m pytest -q model` | 25 s | 80 tests: codec round trip, packet format, change guards; everything else is checked against this model |
| 2 | RTL bit-exact on real recordings | `docker run --rm -v "$PWD:/work" -w /work/test/core nlc-flow make COCOTB_TEST_FILTER=t_if_1` | 1.5 min | 8 real channels, 4 packets, one slot per clock at 5 MHz: every byte equals the model |
| 3 | throughput guarantee | `python scripts/proofs/output_bound.py --d8`, then step 2 with `COCOTB_TEST_FILTER=t_bw_3` | 10 s + 1.5 min | +138 clocks of slack at the real frame for any data; worst-case data on 8 adjacent channels: no abort |
| 4 | a fault, recovered | step 2 with `COCOTB_TEST_FILTER=t_rob_4` | 1 min | a short frame aborts its packet with a token; the next packet is clean and aligned |
| 5 | bring-up | `python scripts/bringup/selftest.py` | 30 s | a single fault anywhere in the datapath is named from the output bytes alone (26/26) |
| 6 | power story | [results.md](results.md) F17-F20 and `reports/latest/summary.md` | 2 min | routed 80 -> 35 uW: the clock tree, not the logic; the core is 24.5 uW, the TT pin interface 9.8 uW |
| 7 | sign-off | the gds run page: gds, precheck, gate-level test green; SDF at 5 corners (F28) | 30 s | 3x2 tiles, +127 ns setup, +0.20 ns hold |

If something fails live: the last full run is in `reports/` (`python nlc.py report`).
