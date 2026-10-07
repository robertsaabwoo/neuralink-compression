# Results: measurements and findings

Numbers behind the summary in the [README](../README.md). Pre-layout, sky130_fd_sc_hd, typical
corner, 5 MHz, 8 channels unless stated. Raw per-run output: `reports/latest/` (`summary.md`,
`metrics.json`) and one JSON per `test/core` test in `test/results/core/`. Last updated 2026-10-07.

## Findings (input to the design phase)

The tests that expose F2-F4 stay as written and fail on today's RTL. `scripts/flow/flow.py`
lists them in `KNOWN_FAIL`, so they are reported without failing the run.

| # | finding | test | constraint |
|---|---|---|---|
| F1 | **Processing latency is 2.0 ms, not ~310 us.** A sample is fully coded only when the last symbol that depends on it is coded. Through the lifting updates and the delta-coded a3, a sample early in a 64-frame block reaches a symbol 36 frames later. Time in the queue alone is 307 us | T-IF-1 | C-LAT-1 (definition open) |
| F2 | **Disable mid-packet leaves an unterminated packet at the host.** The host already has the header and first bytes; the next run's packet 0 is appended (668 = 15 + 653 bytes). Clearing the FIFO would not help: the pins need an abort signal | T-IF-3b, T-ROB-2, T-ROB-3 | C-IF-6/7 |
| F3 | **A host stall corrupts that packet and every later one, with no recovery.** The core `overflow` pin stays 0. A 300-clock stall during the flush is absorbed | T-OVF-1/2/3 | C-OVF-1..3 (D3) |
| F4 | **One short frame misaligns the channels for good.** Long frames and a missing `s_frame` recover | T-ROB-4 | C-IF-8 |
| F5 | **Worst-case host speed: one byte every 11 clocks (2.2 us).** LFSR data codes at 21.4 bits/sample (3.36 Mbit/s); 12 clocks/byte loses data | T-BW-1/2 | C-BW-2/3 |
| F6 | Random power-up contents in all 234 no-reset registers: identical output | T-ROB-5 | C-FN-6 |
| F7 | Power is 98% flip-flop clock pins (618 of 629 uW, also when idle) | T-PWR-1 | C-PWR-1/2 |
| F8 | Critical path 61.9 ns at ss: fine at 200 ns, fails TT's old 20 ns default | T-STA-1 | C-TIM-1 |
| F9 | Yosys and Icarus disagreed on signed SystemVerilog (7,033 vs 670 bytes); fixed with explicit bit slices, now a coding rule | T-GL-1 | C-RTL-2 |

## Budgets

| metric | value | limit / target | status |
|---|---|---|---|
| lossy core area | 128,000 um^2 (2,681 flops, 62% of area) | 173,000 / 86,600 | WARN |
| TT design utilisation (8x2) | 48% | 70% / 60% | PASS |
| setup slack, ss, 200 ns | +138 ns | >= 0 / 60 | PASS |
| power, real data | 628.6 uW | 40 / 16 | FAIL |
| power, idle | 617.8 uW | 10 / 2 | FAIL |
| processing latency | 2.0 ms | 1 / 0.41 ms | FAIL (F1) |
| delivery latency | 13.4 ms | 40 / 20 ms | PASS |
| bits/sample, 160 held-out files | 2.087 | 2.2 / 2.1 | PASS |
| median SNR | 18.0 dB (10th pct 13.3) | 17 / 18 dB | PASS |

## Test status

| suite | result |
|---|---|
| model (pytest, incl. change guard) | 76 pass |
| `test/core` (42 cases, 25 min) | 31 pass, 10 fail (F2-F4), 1 skip (T-ROB-1, `NLC_LONG=1`) |
| `test/lossy` (incl. power scenarios) | 7 pass |
| `test/rans` | 6 pass |
| TT top through the pins, 200 ns | lossy pass; modes 0/2/3 skipped (D1); replay 7/7 |
| gate level: lossy core, TT top netlist | pass, pass |

## Data

**Bandwidth, real data** (T-IF-1, 4 packets): packets 670, 650, 637, 653 bytes; 2.55 bits/sample
on this excerpt, SNR 17.6 dB; out FIFO peak 1; flush 27 bytes in 55 clocks.
Bytes per frame (count of frames): 1: 207, 2: 265, 3: 182, 4: 124, 5: 87, 6: 30, 7: 11, 8: 2,
9: 2, 26-40 (flush frames): 4.

**Bandwidth, worst case** (T-BW-1 LFSR): up to 45 bytes/frame, typically 18-24; flush 30 bytes.

**Host turnaround search** (T-BW-2, LFSR, clocks per byte -> no loss?): 33 no, 17 no, 9 yes,
13 no, 11 yes, 12 no. Result 11, then verified for 2 packets.

**Latency** (T-IF-1): queue (push -> coder) median 103 us, max 307 us; processing (incl.
look-ahead) median 1,664 us, max 1,997 us; delivery 13.4 ms; look-ahead up to 36 frames.

**Power** (gate level, 2 packets from frame 64): op 628.65 uW (sequential 621.5, combinational
7.15); with glitches 628.94; idle 617.83.

**Algorithm** (`algo_eval.py --quick`, 4 held-out groups): 2.08 bits/sample, median SNR 16.6 dB
(subset, reads lower than the 160-file run), spike hit rate 93.9%, false positives 20.6% of
original spikes, max reconstruction error 13 codes, held-out rate 0.7% worse than training.

**Coverage** (docs/testing.md 4.5): 40 of 43 bins. Missing: `seq_wrap` (needs `NLC_LONG=1`),
`occ_7` and `back_to_back_same_channel` (look unreachable as sampled; to decide).
