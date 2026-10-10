# Bring-up: output-diff diagnosis (tool internals)

How to use it on a board, example output, validation and limits: [docs/bringup.md](../../docs/bringup.md).
This file: what each part does.

| file | what |
|---|---|
| `diagnose.py` | the diagnoser: capture + config (+ readback) + stimulus -> findings, verdict, RTL names |
| `datapath.py` | bit-exact model of `nlc_lossy`'s datapath at RTL widths, vectorised fault hooks |
| `rtl_map.py` | report term -> RTL instance / clock-gate / latch cells, parsed from `src/` |
| `patterns.py` | the bring-up pattern set: coverage table, `--emit` board stimuli |
| `selftest.py` | 26 model-level fault cases, no simulator (~30 s, in CI) |
| `fault_table.py` | tables of the RTL fault-injection runs (`test/bringup`) |

## Inputs

- **Capture** (text): whitespace-separated tokens, `#` comments. `XX` a data byte (hex), `XX L`
  a byte with `m_last`, `A` an abort token (`uio[7]` = 1 with `uio[6]` = 0), `OVF` the sticky
  overflow pin `uio[4]` went high. `diagnose.write_capture()` writes this format.
- **Config**: `--slots 1,5,...` or `--config cfg.json` (`{"slots": [...], "readback": {"0x01": 8, ...}}`).
- **Readback** (optional): `--readback 00=00,01=08,10=01,...` (hex), the registers read on
  `uo_out` before enable. Only used when the output is dead: it splits the pin front end
  (reset / strobe / cfg_en synchronisers) from the frame path.
- **Stimulus**: `--stim` `.npy`/`.csv`, full frames `(T, n_slots)` (256 on the core, 32 on the
  TT pin path; any width > max slot) or the selected channels `(T, n_sel)`. Full frames let
  the tool say which slot a broken channel carries. Or `--source ramp|lfsr|constant|real`.
  Packet k covers frames [256k, 256k+256); `--align N` searches the offset of packet 0.

## How it localises

1. **Stream.** Framing on `m_last`, header mode/seq, seq gaps (after an abort token: D5-D7
   protocol event; without: lost packet), a data pin constant although the expected stream
   toggles it, no `m_last`, no output (then: readback dead -> front end; overflow set -> coder
   stall; readback fine -> frame path).
2. **Packet.** First differing byte against the golden model (`model/nlc`), and the symbol
   being coded then.
3. **Symbols.** Packets are independent and rANS decoding is a bijection, so a packet from a
   chip whose coder works decodes cleanly into whatever symbols the chip computed. The tool
   decodes backwards with the model's schedule; if every state returns to L, the coder and
   byte path are fine and each wrong symbol has a channel, context, band index and frame.
   `datapath.py` then tests, each in one vectorised pass:
   - each per-channel latch row (11 per channel: `sx e1 o1 dp1 e2 o2 dp2 e3 o3 dp3 qa`)
     never written, with **all** 2^W power-up values (dead latch enable / clock gate),
   - each single stuck bit in those rows,
   - each stuck bit of 19 shared nets / registers (~470 hypotheses in one batch): the sample
     `x0`, `sg_sx`, the shared lifter's ports `lx le lo ldp la ld`, quantisers, `da3`, the
     inter-level register `xr`, the issued value `hv`, the staging registers
     `sg_xq sg_e2 sg_e3 sg_dp`,
   - each channel fed from any slot, in time or one frame late ("late": the sample row is read
     before it is written, i.e. the sample got the wrong channel index).
4. **Coder.** If the capture does not decode, the fault is in the coder or the byte stream.
   Re-encoding the expected symbols with: one dropped / inserted / corrupted byte, a state-bit
   flip (every earlier symbol of the channel, every bit), a flush-only difference, a ROM entry
   bit (entries used before the first wrong byte), a write-back bit, a bit of the divider loop
   register `x_l` (a bit-exact model of the looped DIV_K = 5 divider, `rtl_divide`), a stuck
   state-row bit. Otherwise: the clean forward prefix and backward suffix bound it.
5. **Reverse lookup.** `rtl_map.py` parses `src/` (instance chain from `project.v`, the
   `g_ch` / `g_st` generate loops, `nlc_rreg -> g_l.u_r` latch rows, pin front end), so the
   names follow the RTL. `diagnose.py --rtl-map` prints the table.

Each verdict has a confidence: `exact` (a modelled fault reproduces every captured symbol /
byte; equivalents listed), `ambiguous` (two explanations, both printed), `high`, `medium`,
`low`, and a next step.

## RTL fault injection (`test/bringup`)

`nlc_fault_top.v` is a **test-only** wrapper around `nlc_core` (not in `src/`, never
synthesised) with `force` hooks for combinational nets; registers and latch rows get cocotb
Force / Deposit or a stuck-bit watcher (`test_faults.stick`). `test_pins.py` drives the TT top
(`test/tb.v`) through the pins. Per fault: capture, stimulus, config, diagnosis, result in
`test/results/bringup/<test>/`; `fault_table.py` makes the tables in docs/bringup.md.
