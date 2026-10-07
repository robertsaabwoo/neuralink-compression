# Handoff: lossy mode (DWT + rANS) in hardware

## RESUME HERE (updated 2026-10-07, "build the actual tests" mostly done)

All data now comes from `python nlc.py algo|sim|synth|sta|gate_sim|power|all|report|accept`
(wraps `scripts/check.py` -> `scripts/flow/flow.py` in the `nlc-flow` image). Rules unchanged:
testing phase only, no compressor RTL/algorithm design (test peripherals OK). Specs:
`docs/constraints.md` (C-*, D1-D4), `docs/testing.md` section 4. Results and findings F1-F6:
`docs/testing.md` section 3.

**Done this session (2026-10-07):**
- `test/env/nlc_env.py`: shared environment for `nlc_core` (ADC 256 slots/clock with filler and
  frame-length faults, cfg port, host model with turnaround/p_ready/stalls, scoreboard by seq with
  allow_loss/grace/resync, RTL monitors for latency/bandwidth/coverage, JSON per test).
- `test/core/` (42 cases, 25 min): 31 pass, 10 fail = findings F2 (disable mid-packet ->
  unterminated packet), F3 (host stall -> no recovery), F4 (short frame -> permanent misalign);
  listed in `KNOWN_FAIL` in flow.py. T-BW-2 measured C-BW-3 = 11 clocks/byte. F1: processing
  latency incl. transform look-ahead is 2.0 ms (queue only 307 us): **ask the user which
  definition C-LAT-1 uses** before scoring it.
- TT top at 200 ns, SLOT_CYCLES 2, 32-slot vectors; modes 0/2/3 skip (D1) except replay;
  `make -C test GATES=local NETLIST=...` = T-GL-2 (pass).
- `test/lossy` power scenarios (`test_power_op/n4/worst/floor`), `gl_dump.v +vcd_start`.
- flow.py steps `core`, `area` (N_SEL sweep), `algo`, TT-top GL in `gl`, power scenarios in
  `power`; budgets for latency, idle/worst power, coverage. `.github/workflows/model.yml` runs the
  TT top and core T-IF tests. Flow verified with `--only synth,gl,power,algo --quick`.

**To do:**
1. Run `python nlc.py all` once (never run end to end with the new steps: `core` full, `area`,
   power `n4/worst/floor`, `algo --full`), then `--update-baseline`. Check that the 5% rule
   between packets (4.4) holds for power; it is not computed yet.
2. `NLC_LONG=1` once: T-ROB-1 (seq wrap, fills the `seq_wrap` coverage bin) and the T-OVF-2 matrix.
3. Decide with the user: C-LAT-1 definition (F1); coverage bins `occ_7` and
   `back_to_back_same_channel` (look unreachable as sampled).
4. Optional: `src/nlc_stimgen.v` + T-GEN-1/2, T-IF-5 (not in info.yaml/project.v yet);
   T-FV-1/T-EQ-1 need SMT solvers in the image.
5. Speed: Icarus + per-clock cocotb loop runs ~3,200 clocks/s (1 packet ~ 21 s).
Docker image lacks SMT solvers and has Verilator 5.032 (< cocotb 2.1's 5.036): no formal/sby,
no Verilator sims. Write files with the Write tool (bash heredocs mangled `\n` in Python).

Last updated 2026-10-07.

**Current direction (2026-10-07):** algorithm/design work is paused. Order of work:
(1) platform understanding - done in `docs/platform.md` (Neuralink architecture from the
patents/paper, TT as the constraint envelope); (2) good testing; (3) design. Until step 3,
RTL is limited to testbenches and peripherals (e.g. an on-chip 256-slot stimulus generator,
`docs/platform.md` 4.4). The clock-gating plan (`~/.claude/plans/this-is-a-question-transient-hammock.md`)
is on hold for the design phase; none of it was implemented.

## Goal

A low-area, low-power Tiny Tapeout (sky130) implementation of Neuralink's
lossy compression: bior2.2 (LeGall 5/3) wavelet -> quantisation -> rANS with
static, offline-trained tables (the patent's lossy mode). The point is the
silicon cost, not the ratio. Budgets and current numbers: `docs/budgets.md`.

## Design (decided)

- **Clock = ADC slot rate (5 MHz)**, one sample per clock at most, never stalls the ADC.
- **Streaming wavelet, no block buffer.** 3 levels on 64-sample blocks; each
  level is a lifting stage with 3 registers per channel. One combinational
  datapath (`nlc_lift53` x3) shared by all channels; per-channel state is
  register rows. All channels are at the same block position, so the schedule is global.
- **Per-channel FIFO (8 entries).** A sample pushes 0-4 symbols but one on
  average; each channel pops one per sample, so the coder sees one symbol per
  input sample, round-robin (no pipeline hazard). Peak 7; 6-frame lag.
- **rANS (`nlc_rans`)**: II=1 pipeline, one state X per channel; f_s, c_s,
  M = 4096 constant (4 tables in a synthesised ROM, `nlc_lossy_rom.v`); 64
  symbols + ESC with 2 raw bytes; lsh = 2 (22-bit state, 13-deep pipeline,
  3-byte flush); hazard check built into `s_tready`.
- **Packet** = 4 blocks (256 frames): header {mode=1, seq}, renorm/raw bytes,
  8 x 3-byte final states. Spec: docstrings of `model/nlc/lossy.py` and `rans_tdm.py`.
- **Integrated as mode 1** in `src/nlc_encoder.v` (modes 0/2/3 are still the stub).

## Repository state

- **Fork of TT's `ttsky-verilog-template` (TTSKY26d)**: remote `upstream`,
  branch `main` based on `upstream/main`, our files uncommitted on top.
- **Pending (needs you)**: restoring the template files into the working tree
  was blocked by the permission system. Run:
  ```
  git checkout -- .devcontainer .github/workflows/docs.yaml .github/workflows/fpga.yaml \
    .github/workflows/gds.yaml .github/workflows/test.yaml .vscode LICENSE docs/info.md \
    src/config.json test/README.md test/requirements.txt test/tb.gtkw
  ```
  then review `git status` (we deliberately differ in `.gitignore`, `README.md`,
  `info.yaml`, `src/project.v`, `test/Makefile`, `test/tb.v`; the template's
  `test/test.py` is unused), commit, create the GitHub repo and push. The `gds`
  action then gives the sign-off area/timing/precheck.
- `info.yaml`: tiles `8x2`, language SystemVerilog, lossy sources listed.
- Tests ported to **cocotb 2.1** (the template's version).
- Nothing committed yet.

## Check flow (new)

```
python docker/fetch_inputs.py && docker build -t nlc-flow docker   # once (~10 min, 1.3 GB)
python scripts/check.py                                            # everything, ~12 min
python scripts/check.py --only synth,sta --quick                   # steps: model lint rtl synth sta gl power compress
python scripts/check.py --update-baseline                          # record known-good values
```

Report: `reports/latest/summary.md` (PASS/WARN/FAIL table vs `scripts/flow/budgets.json`,
details) plus every log, netlist, STA/power report. Image: Icarus, cocotb 2.1, Verilator,
Yosys 0.52, OpenSTA 3.1 (built from source: Debian's 2.0.17 reports zero internal power),
sky130_fd_sc_hd at tt/ss/ff + gate-level models. Inputs are fetched on the host because
TLS inside containers fails on this machine; on Windows `git` needs
`-c http.sslBackend=schannel -c http.schannelCheckRevoke=false`.

Power method: gate-level sim of one packet of real data at 5 MHz -> VCD of the netlist's
nets -> per-pin activity sampled per clock (`scripts/flow/vcd_activity.py`, glitch-free;
glitches are reported separately) -> OpenSTA `report_power`.

## Current numbers (baseline)

| | value | budget |
|---|---|---|
| tests | all pass (4 TT-top tests pending: modes 0/2/3 unimplemented) | PASS |
| compression | 2.087 bits/sample, 18.0 dB median SNR | PASS |
| lossy area | 128,000 um^2, 0.016 mm^2/ch, 62% flops | WARN (target 4x2) |
| TT design | 48% of 8x2 | PASS |
| critical path (ss) | 61.9 ns: **fails 20 ns** by 41.9 ns; +138 ns at 200 ns | **FAIL** at TT default |
| power @ 5 MHz | **628 uW**: 618 uW flop clock pins, ~11 uW data | **FAIL** (limit 80) |

## Findings this session

1. **Yosys and Icarus disagreed on signed SystemVerilog** (size casts of signed
   values / signed function arguments): the synthesised netlist produced 7,033
   bytes instead of 670. Fixed by writing sign extension and arithmetic shifts as
   explicit bit slices (`nlc_lift53.sv`, `quant` in `nlc_lossy.sv`). Rule: no signed
   casts, `>>>` or signed compares in synthesised code; the gate-level step catches it.
2. **Power is 98% flip-flop clock power.** Fix: clock gating.
3. **The single-cycle front end is ~60 cells deep**: fine at 5 MHz, not at TT's 50 MHz default.

## Next steps

Start every session with `python scripts/status.py` (direction, repo state, last results,
budgets, open items). Order agreed with the user: platform (done) -> testing -> design.

1. **User**: run the pending `git checkout -- ...` (printed by `status.py repo`), commit, push.
2. **Testing phase** - requirements are `docs/constraints.md` (decisions D1-D4, 2026-10-06:
   lossy only, 5 MHz one slot/clock, packet-granular overflow, Neuralink-derived power 40/16 uW).
   Build the verification plan in `docs/testing.md` section 4 in the order of 4.7. Original summary:
   interface conformance at the real 256-slot interface, the on-chip stimulus generator
   peripheral (+ model), latency/bandwidth/power against Neuralink's numbers, robustness and
   coverage, CI. Revisit `scripts/flow/budgets.json` power budget with the Neuralink-derived
   figure (`docs/platform.md` section 1; radio energy/bit is unsourced).
3. **Design phase (later)**: clock gating (plan on file), clock/`CLOCK_PERIOD`, area toward
   4x2, modes 0/2/3. Measured problems to address then: power 98% flop clock pins; critical
   path 62 ns vs TT's default 20 ns.
