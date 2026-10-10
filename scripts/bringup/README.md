# Bring-up: output-diff diagnosis

`diagnose.py` reads the chip's output bytes, the configuration and the stimulus you drove,
and says what is broken. It works down from stream framing to the register row or shared net
at fault, and gives the RTL instance name (and the clock-gate cell it implies in the netlist).
It runs on the host in plain Python (numpy only) and needs no simulator.

```
python scripts/bringup/diagnose.py --capture cap.txt --slots 3,40,41,90,128,200,254,255 \
       --stim frames.npy [--frame-offset N | --align 300] [--json out.json]
python scripts/bringup/diagnose.py --rtl-map        # report term -> RTL instance table
python scripts/bringup/selftest.py                  # 16 model-level fault cases, ~15 s
python scripts/bringup/fault_table.py               # table of the RTL fault-injection run
make -C test/bringup                                # RTL fault injection (Docker: nlc-flow)
```

## Inputs

- **Capture** (text): whitespace-separated tokens, with `#` comments. `XX` is a data byte in hex.
  `XX L` (or `XXL`) is a byte that had `m_last` set. `A` is an abort token. On the TT pins an
  abort token is `uio[7]` (m_last) = 1 while `uio[6]` (m_valid) = 0; you ack it like a byte.
  `diagnose.write_capture()` writes this format.
- **Config**: `--slots` (n_sel is the number of slots given) or `--config cfg.json` containing
  `{"slots": [...]}`.
- **Stimulus**: `--stim` takes a `.npy`/`.csv` file. Shape (T, 256) is full ADC frames, where
  row t is frame t after enable and column s is slot s. Shape (T, n_sel) is the selected
  channels only. Full frames are better: with them, the tool can say which slot a broken
  channel actually carries. You can also pass `--source ramp|lfsr|constant`, which uses the
  on-chip generator patterns (full frames), or `--source real --offset N`.
  Packet k covers frames [256k, 256k+256). If you don't know which frame enable landed on, use
  `--align N`, which searches offsets 0..N against the first packet.

## What it reports

1. **Stream level.** Framing on m_last. Header mode and seq. Seq gaps, either after an abort
   token (a D6/D7 protocol event: host stall, short frame or enable drop) or without one
   (packet loss). Abort tokens. Lengths against the model. No output, output stuck at one
   value, m_last never set, trailing partial packet. A data pin stuck: a bit that is constant
   in every captured byte although the expected stream toggles it.
2. **Packet level.** The first differing byte against the golden model
   (`model/nlc/lossy.py` + `packet.py`), and which symbol was being coded when it went out.
3. **Symbol level.** See "How it localises" below. The result names the channel, the
   context (d1/d2/d3/a3), the band index, the block and the frame, then the hardware block:
   - **row stuck** (dead clock gate). Example: "channel 3, row dp2 never written, holds 1445"
     maps to `core.u_enc.u_lossy.g_ch[3].u_dp2`, gate `...g_ch[3].u_dp2.u_icg.u_cg`.
   - a stuck bit in a row
   - a stuck-at on a shared net, e.g. `u_l2.d[3]` (level-2 lifter), the quantisers, the a3
     delta, the issuer read mux, or the sample input x0
   - **slot selector**: "channel 2 carries slot 42 (configured 41)", or a permutation, which
     means channel order is wrong. "(late)" means that channel's burst row is written after
     the issuer has read it, so it codes the previous frame's burst. That is what a wrong
     channel index looks like.
   - **rANS state flip**: channel, bit, and the symbol (frame) it happened before
   - **rANS ROM entry**: (context, symbol), field f_s/c_s, bit. **Write-back** wb_x bit stuck
     (divider/adder, shared). **State row** bit stuck (one channel, permanent).
   - **final-state flush** wrong for channel c (state row c or the flush mux)
   - **byte path**: a byte dropped, inserted or corrupted downstream of the coder (FIFO,
     pins, capture)
   - **header**: a seq bit (constant XOR, so a stuck bit) or a seq offset (constant, so a
     register upset)
4. **Confidence and next step.** Each verdict has a confidence level. `exact` means a modelled
   fault reproduces every captured symbol or byte. `ambiguous` means two explanations
   reproduce it, and both are printed. `high`, `medium` and `low` are lower levels. Each
   verdict also says what to probe or re-run.
5. **Reverse lookup.** `rtl_map.py` parses `src/` (the instance chain from `project.v` down,
   and the `g_ch` / `g_st` generate loops), so report terms always match the RTL as it is
   today. The full table is in `--rtl-map`.

## How it localises

Packets are independent: blocks and rANS states restart at every packet. rANS decoding is a
bijection, so a packet from a chip whose coder works decodes cleanly into whatever symbols
the chip computed, even when those symbols are wrong. The tool decodes the capture
backwards with the model's schedule (`coding_order`). If the stream is consumed exactly and
every state returns to L, the coder and the byte path are fine. Each symbol that differs from
the model then has a channel, a context and a frame. Next, `datapath.py` is used. It is a
bit-exact model of `nlc_lossy`'s datapath at RTL widths (checked against `lossy.py`). It is
vectorised, so it tests each hypothesis against the decoded symbols in one pass:

- every per-channel row (14 per channel) never written, with **all** 2^W power-up values
  tried at once
- every single stuck bit in those rows
- every stuck bit of every shared net (13 nets, about 320 hypotheses in one batch)
- each channel fed from any of the 256 slots

A hypothesis that reproduces all symbols exactly is reported, and so is every equivalent one.

If the capture does **not** decode, the fault is in the coder or the byte stream. The tool
re-encodes the expected symbols and tries these explanations:

- a single state-bit flip on the channel whose bytes went wrong first, at every earlier symbol
  and every bit, with early exit
- one dropped, inserted or corrupted byte
- a flush-only difference
- a ROM entry bit (f_s or c_s) for each entry used just before the first wrong byte
- a write-back bit stuck, on all channels
- a stuck bit in one channel's state row

If none of them fits, it gives the clean forward prefix and the clean backward suffix, which
bound the fault.

## What it cannot localise (and why)

- **Faults the stimulus does not exercise.** A stuck bit that the data never toggles leaves
  no trace. Equivalent hypotheses that give identical output for this stimulus are all listed,
  and the tool cannot pick between them. Fix: re-run with a stimulus that toggles the bit
  (ramp, LFSR, full-scale).
- **Several simultaneous faults.** Only single-fault hypotheses are modelled. With two faults
  you get the symbol-level description (channels, contexts, first frame) and a
  signature-based guess, at confidence `low`.
- **State flip versus byte corruption.** A flip in state bits that are emitted at once is
  byte-for-byte the same as a corrupted output byte. The tool reports both as `ambiguous`.
- **Coder control faults.** Examples: the issuer sending the wrong context or channel, a
  handshake or hazard bug, a divider fault that does not show as one stuck write-back bit,
  or several ROM bits at once. These are bounded (clean prefix and suffix, channel, frame)
  but not named. The ROM entry, write-back bit, state-row bit and single state flip cases
  are named exactly.
- **Rows read before their first write** (only under a channel-index fault) hold power-up
  contents. Those few symbols are wildcards in the "(late)" slot match.
- **Timing.** The capture has no clock times, so latency faults show only through their
  effect on the data (for example abort tokens).
- A row that is stuck *at its correct value* for this data is invisible, and so is a fault in
  a channel that is not selected.

## Observability hook that would close the remaining gaps (not implemented)

The coder control faults above are bounded but not named, because the output carries only
rANS bytes. A debug mode that sends the issued symbol stream {rr, hctx, hsym, raw} uncoded
would name them: the datapath would be checked symbol by symbol, independently of the coder.
It is a new output format, though, even if only in debug mode, so per the guardrails it is a
decision for the user and has not been implemented. The production format stays bit-exact.

## Validation (RTL fault injection, test/bringup)

`test/bringup/nlc_fault_top.v` is a **test-only** wrapper around `nlc_core`. It is not in
`src/` and never goes to synthesis. It names the core instance `core`, as `project.v` does,
and has `force` hooks for combinational nets. Faults on registers are cocotb Force/Deposit.
Each test plays real data through the 256-slot interface, captures bytes the way the host
would, runs the diagnoser and checks the verdict. Per-fault capture, stimulus and report go to
`test/results/bringup/<test>/`, and `fault_table.py` regenerates this table. The run below
used e13 RTL (Icarus 12, cocotb 2.1). f_a_gclk ran with `ICG_SIM=yes`. f_e_* corrupt the
clean RTL capture the way a board would see it.

| test | injected fault | tool's verdict | correct | precision |
|---|---|---|---|---|
| f_clean | none | no fault found | yes | - |
| f_stall | host stall 3000 clocks at byte 200 (control) | [high] seq gap after an abort token: packets [0] lost (seq 0..0) | yes | abort token + lost packet 0 named; no fault claimed |
| f_a_dp2 | g_ch[3].u_dp2.q stuck at 0x5a5 (dead gate) | [exact] channel 3, row dp2 (level-2 lifter: previous detail d2[i-1]) never written - holds value 1445: reproduces all 256 symbols of channel 3 exactly | yes | channel 3, row dp2, and its power-up value |
| f_a_o1 | g_ch[5].u_o1.q stuck at 0x123 (dead gate) | [exact] channel 5, row o1 (level-1 lifter: pending odd sample) never written - holds value 291: reproduces all 256 symbols of channel 5 exactly | yes | channel 5, row o1, and its power-up value |
| f_a_b3 | g_ch[0].u_b3.q stuck at 7 (dead gate) | [exact] channel 0, row b3 (burst buffer slot 3 (a3 delta)) never written - holds value 7: reproduces all 256 symbols of channel 0 exactly | yes | channel 0, row b3, and its power-up value |
| f_a_gclk | g_ch[3].u_dp2 GCLK forced 0 (CLK pin open) | [exact] channel 3, row dp2 (level-2 lifter: previous detail d2[i-1]) never written - holds value 707: reproduces all 256 symbols of channel 3 exactly | yes | channel 3, row dp2, its clock gate cell named |
| f_b_d2 | u_l2.d[3] stuck-at-1 | [exact] shared net stuck-at reproduces all symbols of all channels exactly: d2[3] stuck-at-1 | yes | net d2 and the bit (equivalent candidates listed) |
| f_b_a1 | u_l1.a[5] stuck-at-0 | [exact] shared net stuck-at reproduces all symbols of all channels exactly: a1[5] stuck-at-0 | yes | net a1 and the bit (equivalent candidates listed) |
| f_c_state | g_st[4].u_st.q bit 17 flipped at frame 100 | [exact] packet 0: rANS state of channel 4 had bit 17 flipped just before symbol 764 (channel 4, d1[17] of block 1, frame 100); re-encoding the expected symbols with that flip reproduces the whole packet | yes | channel 4, state bit 17, the symbol it happened before (frame 100) |
| f_c_rom | ROM entry (d1, sym 31): in_fc bit 4 (c_s[4]) flipped | [exact] packet 0: ROM entry d1 symbol 31 (value 0): c[4] stuck-at-0 - re-encoding with that table entry reproduces the packet | yes | ROM entry (context, symbol), field and bit |
| f_c_wb | u_rans.wb_x[9] stuck-at-1 | [exact] packet 0: coder write-back wb_x[9] stuck-at-1 reproduces the packet (all channels) | yes | the write-back bit and polarity |
| f_d_slot | u_sel.want + 1 for channel 2 (slot 42) | [exact] channel 2 carries slot 42 (configured 41) | yes | channel 2 and the slot it actually carries |
| f_d_swap | smp_ch 1 <-> 6 swapped | [exact] channel 1 carries slot 254 (late) (configured 40); channel 6 carries slot 40 (configured 254) - a permutation of the selected slots: channel index/order (smp_ch); 'late' = that channel's row is written after the issuer read it, so it codes the previ... | yes | both channels, the slots they carry, 'channel index/order' |
| f_e_drop | stream byte 777 (packet 1) dropped | [high] packet 1: byte 107 (0x03) missing, everything else exact | yes | packet and byte index |
| f_e_corrupt | bit 2 of stream byte 1020 flipped | [ambiguous] packet 1: corrupted byte(s) on an otherwise exact packet: byte 350: 0x71 -> 0x75 (bits [2]) | yes | packet, byte index and bit (or 'ambiguous' with a state flip) |
| f_e_pin5 | uo_out[5] stuck-at-0 | [high] data bit 5 is 0 in every captured byte (expected to toggle): uo_out[5] stuck-at-0 | yes | the pin and the polarity |
| f_f_seqbit | header seq bit 1 stuck-at-1 | [high] header seq bit 1 stuck-at-1: same single-bit XOR on 2 packets (payloads are the right packets) | yes | header seq, the bit (payload identified as the right packet) |
| f_f_sequpset | seq register +5 after packet 0 | [high] header seq offset constant (+5) from packet 1 over 2 packets, payloads continuous: seq register upset or loaded wrong, then counting normally | yes | header seq of packets 1, 2; constant offset = register upset |

18/18 correct
