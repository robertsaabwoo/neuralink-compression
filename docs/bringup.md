# Bring-up: from a TT board capture to the faulty cell

A chip back from the shuttle either matches the golden model bit for bit or it does not. When
it does not, `scripts/bringup/diagnose.py` takes what the board can see (the output bytes,
the config readback, the overflow pin) and the stimulus that was driven. It names the block,
and usually the register row, net or cell, using RTL instance names and the netlist cells
they become. It is plain Python (numpy) on the host, with no simulator and no debug hardware.
Internals: [scripts/bringup/README.md](../scripts/bringup/README.md).

## On the board (TT demo board, RP2040 drives clk and the pins)

1. **Stimulus.** `python scripts/bringup/patterns.py --emit stim/` writes 14 patterns. Each
   is one packet (256 frames x 32 slots) with the pattern in slots `1,5,6,12,17,22,30,31` and
   seeded filler elsewhere. Together they hit 64/64 symbols in all 4 contexts and drive the
   lifter to full-scale values (`patterns.py` prints the table). Start with `real` and
   `synthetic`, then `lfsr`, `sine_fs` and `noise_amp` for coverage.
2. **Reset and config** (pin map: `src/project.v`). Release `rst_n`. Write `CTRL = 0x01`,
   `N_SEL = 8` and `SEL_SLOT+i`: `cfg_en` = 1, strobe the address byte, then the data byte.
3. **Read back** every register you wrote, before enable: `cfg_en` = 1, strobe the address,
   wait at least 4 clocks, read `uo_out`, drop `cfg_en`. Save the values as
   `{"readback": {"0x01": 8, ...}}` in the config JSON.
4. **Enable** (`CTRL = 0x81`), then strobe the frames: one slot per 2 clocks, `s_frame` with
   slot 0. Strobe a few filler frames after them so the last packet flushes.
5. **Capture every clock** (D8: there is no ack, and a missed clock is a lost byte). Record
   `uio[6]` = 1 as the byte `uo_out` (`XX`, or `XX L` when `uio[7]` = 1); record `uio[7]` = 1
   with `uio[6]` = 0 as `A` (abort token); record `uio[4]` rising as `OVF`.
6. **Diagnose:**

```
python scripts/bringup/diagnose.py --capture cap.txt --config cfg.json --stim stim/real.npy
#   [--align 300]  if packet 0 is not stimulus frame 0
#   --json out.json   findings for scripting;  --rtl-map   the report-term -> RTL table
```

The exit status is 0 when there is no fault (or only protocol events: abort, seq gap) and 1
otherwise. Packet 0 is 256 x 64 clocks = 16,384 clocks of capture.

## Example output (RTL fault injection: channel 3's dp2 latch row, clock gate output held low)

```
capture: 687 bytes, 1 packets, 0 abort tokens, overflow pin low; config n_sel=8 ...
0/1 complete packets exact

VERDICT [exact]: channel 3, row dp2 (level-2 lifting: previous detail d2[i-1]) never written -
  holds value 707: reproduces all 256 symbols of channel 3 exactly
  rtl: core.u_enc.u_lossy.g_ch[3].u_dp2
       (netlist gate core.u_enc.u_lossy.g_ch[3].u_dp2.g_l.u_r.u_icg.u_cg)  [src/nlc_lossy.sv:320]
  next: dead latch enable: check the row's clock gate ... (CLK/GATE/GCLK connectivity: the
        OpenROAD CTS disconnect, results.md F27) and its GCLK net to the latches
        ...g_ch[3].u_dp2.g_l.u_r.g_b[*].u_l
symbol-level:
  - packet 0 decodes cleanly (coder and byte path OK) but 40 symbols differ:
    ch 3: 40 symbols in d3/a3, first d3[0] block 0 frame 14
```

The packet still decodes, so the coder and the byte path are fine. Only d3 and a3 of channel 3
are wrong. Of channel 3's 11 rows x all 2^W power-up values, exactly one row and one value
reproduce all 256 symbols. The gate and latch cell names match a Yosys synthesis of this RTL. This is the
failure mode of the open CTS bug (F27: one `g_ch[7]` gate left without CLK).

## Validation

**Model level** (`python scripts/bringup/selftest.py`, ~30 s, runs in CI in the `ci` golden-model
job on synthetic data): 26/26 cases pass on synthetic data and on the challenge recordings
(`NLC_DATA=<wav dir>`).

**RTL fault injection** (`make -C test/bringup` and `make -C test/bringup TOP=tt`, Docker
`nlc-flow`, Icarus 12, cocotb 2.1, final RTL c8fd3b5). Each test injects one fault into the
RTL that will be taped out, plays real data, captures what a host would see and checks the
verdict. The core bench drives `nlc_core` at the 256-slot interface through a test-only
wrapper. The TT bench drives the full TT top through the pins: config, readback, 32-slot
frames. The three byte-path / output-pin cases change the clean RTL capture instead (a board
or capture fault). **29/29 correct: 26 faults + 3 controls.**

| fault class | injected | diagnosed | how precisely | confidence |
|---|---|---|---|---|
| latch row never written (sx, o1, dp2) | 3 | 3 | channel, row, power-up value | exact |
| latch enable dead (row clock gate GCLK = 0) | 1 | 1 | channel, row, gate and latch cells | exact |
| latch row bit stuck | 1 | 1 | channel, row, bit, polarity | exact |
| shared lifter output (`u_lift.d`, `u_lift.a`) | 2 | 2 | net, bit, polarity | exact |
| staging register bit (`sg_dp`) | 1 | 1 | net + 1 equivalent (`ldp`) | exact |
| rANS state upset | 1 | 1 | channel, bit, symbol / frame | exact |
| rANS ROM entry bit | 1 | 1 | context, symbol, field, bit | exact |
| divider write-back bit | 1 | 1 | bit, polarity | exact |
| looped divider data (`x_l` bit) | 1 | 1 | bit, polarity | exact |
| looped divider control (`it_v` stuck) | 1 | 1 | block only: coder sequencer, 4 candidates | high |
| slot selector (wrong slot, swapped index) | 2 | 2 | channel(s) and the slot carried | exact |
| sample capture flop (`hi_q[0]`, TT pins) | 1 | 1 | sample bit + 1 equivalent | exact |
| reset synchroniser (`rst_q[1]` stuck-at-0) | 1 | 1 | front end, 5 candidates | high |
| input-sync flop (`stb_q[1]`, `frm_q`) | 2 | 2 | front end / frame path, 5 candidates each | high / medium |
| registered output (`last_q`, `data_q[2]`) | 2 | 2 | m_last path; bit + polarity | medium / high |
| output pin, byte dropped / corrupted (capture) | 3 | 3 | pin; packet + byte (+ bit) | high / ambiguous |
| header seq (stuck bit, register upset) | 2 | 2 | bit; offset | high |
| controls: clean (core, TT pins), short frame (D5) | 3 | 3 | no fault; abort + lost packet named | - |

`python scripts/bringup/fault_table.py` regenerates the per-test table (injected fault,
verdict) from `test/results/bringup/`.

## Limits

- **Faults the stimulus does not exercise** leave no trace. Equivalent hypotheses are all
  listed (`sg_dp[6]` is the same as `ldp[6]` on this data). Re-run with another pattern.
- **Single fault only.** With two faults you get the symbol-level description and a
  `low`-confidence guess.
- **State flip vs byte corruption.** A flip in state bits that are emitted at once is byte for
  byte a corrupted output byte. Both are reported, as `ambiguous`.
- **Control faults are bounded, not named:** the divider sequencer (`it_v`/`it_c`), the issuer
  handshake, the pin synchronisers. A dead output tells the front end (readback dead) apart from
  the frame path (readback fine) and from a coder stall (overflow set). Within each group the
  pins cannot separate the reset synchroniser from the strobe / cfg_en synchroniser, or
  `s_frame` from the enable bit. The bit-exact models cover the datapath, ROM, write-back and
  `x_l`, nothing else in the coder.
- **No timing.** The capture has no clock times. Latency or hold faults show only through their
  effect on the data.
- A row stuck at its correct value, or a fault in a channel that is not selected, is invisible.
- Validation is RTL fault injection (forces, deposits). It is not a gate-level fault simulation of
  the netlist. The swtest study's sampled stuck-at run was on the pre-D8 protocol and has not been
  redone.

## What the DFT modes would add (branch area4-final-dft, not used here)

- **Raw bypass** (issued symbols out uncoded) checks the datapath symbol by symbol without the
  coder. That names the coder control faults that are only bounded today, and settles the
  state-flip vs byte ambiguity.
- **Clock-gate-enable observation** shows a dead gate directly. Today a dead gate is told apart
  from a row that is written but wrong only through the power-up value fit, and a gate
  stuck open costs only power and is invisible in the bytes.
