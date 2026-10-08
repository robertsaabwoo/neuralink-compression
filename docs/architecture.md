# Architecture (as built, 2026-10-07)

What the RTL does today. Requirements: [constraints.md](constraints.md). Numbers:
[results.md](results.md). Decisions D5-D7 are **not implemented yet**; where they land is marked.

```
 TT pins ─► project.v ─► nlc_core ──────────────────────────────────────────────► TT pins
          (pin protocol)  ├─ nlc_cfg       config registers
                          ├─ nlc_slot_sel  256-slot ADC stream -> up to 8 channels
                          ├─ nlc_encoder   wrapper; mode 1 = nlc_lossy:
                          │    wavelet -> quantise -> per-channel FIFO -> rANS -> header + bytes
                          └─ nlc_out_fifo  8-byte output FIFO -> host
```

One clock, 5 MHz = the ADC slot rate (D2). At most one sample per clock; the ADC is never stalled.

## Blocks

**`project.v` (TT top).** Registers all inputs once; `s_strobe` and `m_ack` are edge-detected
into one-clock pulses. A strobe carries one 10-bit sample (`ui_in` + `uio[1:0]`, `s_frame` =
slot 0), or a config byte (address, then data) while `cfg_en` = 1. Output: byte on `uo_out`,
`m_valid`/`m_last` on `uio[6]/[7]`, host pulses `m_ack`. The pin path manages one sample per 2
clocks at most: it is for slow real-data tests (C-IF-9).

**`nlc_cfg`.** Registers: mode, enable, `n_sel` (1-8), slot per channel (ascending), plus
fields for the model-only modes 0/2/3. `enable` = 0 clears the core.

**`nlc_slot_sel`.** Counts slots (restart on `s_frame`), emits the configured slots as channels
0..n_sel-1 with first/last-of-frame flags, starting at the first `s_frame` after enable.
*D5 lands here:* today a short frame loses channels and the encoder detects "frame done" from
the last channel, which misaligns everything (F4).

**`nlc_lossy` (inside `nlc_encoder`; modes 0/2/3 are stubs, D1).**
1. *Wavelet:* 3 levels of LeGall 5/3 lifting (`nlc_lift53` x3), streaming, 64-sample blocks.
   One shared combinational datapath; per-channel state = 3 registers per level, in rows
   indexed by channel (time-multiplexed). All channels share one block position, so the
   schedule is global.
2. *Quantise:* right shifts d1 >> 3, d2 >> 2, d3 >> 2, a3 >> 1; a3 is delta-coded. Explicit
   bit slices, no signed casts (C-RTL-2).
3. *Per-channel FIFO*, 8 x 13 bits per channel: a sample pushes 0/1/2/4 symbols, the coder
   pops one per sample, round-robin over channels. Peak 7, lag ~6 frames (307 us).
4. *rANS (`nlc_rans`):* one 22-bit state per channel, one symbol per clock, 13-stage
   pipeline (bit-serial divider). Static tables in a synthesised ROM (`nlc_lossy_rom.v`):
   4 contexts, 64 symbols + escape (escape = 2 raw bytes). 0-4 bytes per symbol. Packet end:
   flush all 8 states, 3 bytes each.
5. *Serialiser:* header `{mode = 1, seq[5:0]}`, then the coder bytes, `m_last` on the last.

A packet = 4 blocks = 256 frames (13.1 ms), ~650 bytes on real data, up to ~5,500 on noise.
Packets decode independently. Format spec: docstrings of `model/nlc/lossy.py`, `rans_tdm.py`.

**`nlc_out_fifo`.** 8 bytes, valid/ready. When full, the encoder is held, back-pressure reaches
the per-channel FIFOs and they overwrite (F3). *D6/D7 land here and in the serialiser:* the
`m_abort` token, flush on a blocked output, abort on disable.

## Cost today

| | value |
|---|---|
| area | 128,000 um^2 lossy core; 48% of an 8x2 TT design |
| flip-flops | 2,681 (62% of area) |
| state per channel | ~240 b: 114 wavelet + 104 FIFO + 22 coder (Neuralink spike path: 226 b/ch) |
| timing | 62 ns critical path (fine at 200 ns) |
| power | 629 uW, 618 of it flop clock pins (budget 40 uW) |

Main design-phase lever: power is clock power. Each channel's state changes on 8 of 256 clocks,
but every flop is clocked every cycle: clock gating or denser storage (latches, SRAM).

## Where to change what

| change | files | tests that must stay green |
|---|---|---|
| algorithm (shifts, block, tables) | `model/nlc/lossy.py`, then RTL constants, ROM via `scripts/gen_lossy_rom.py` | `pytest` (change guard), `test/lossy`, `test/core` |
| D5 frame rule | `nlc_slot_sel.v`, frame counting in `nlc_lossy.sv` | T-ROB-4 |
| D6/D7 abort, flush, reset | `nlc_out_fifo.v`, serialiser in `nlc_lossy.sv`, new `m_abort` port on `nlc_core`, pin encoding in `project.v` | T-OVF-1/2, T-IF-3b, T-ROB-2/3/7 |
| power (clock gating) | per-channel registers in `nlc_lossy.sv`, `nlc_rans.sv` | everything + `nlc.py power` |
