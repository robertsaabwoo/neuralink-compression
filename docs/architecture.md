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

**`project.v` (TT top).** Pin CDC: `rst_n` goes through a 2-flop reset synchroniser (async
assert, sync release; the synchronised reset is the only reset inside the chip); `s_strobe`
and `cfg_en` through 2-flop synchronisers, the strobe edge-detected on stage 2/3; `s_frame`
sampled with strobe stage 1; the sample bus loaded once, one clock after strobe stage 1, while
the host still holds it, by a clock gate whose enable is built from flop outputs only (no raw
pin reaches a gate enable; the stage-1 MTBF argument is in `project.v`). A strobe carries one
10-bit sample (`ui_in` + `uio[1:0]`, `s_frame` = slot 0), or a config byte (address, then
data) while `cfg_en` = 1. Output: byte on `uo_out`, `m_valid`/`m_last` on `uio[6]/[7]`, all
from one output register stage (D8: no ack). The pin path manages one sample per 2 clocks at
most (C-IF-9).

**`nlc_cfg`.** Registers: enable (CTRL[7]; the mode bits CTRL[1:0] are ignored, lossy only),
`n_sel` (1-8), slot per channel (ascending). `enable` = 0 clears the core. The registers of
the model-only modes 0/2/3 were removed from the RTL (2026-10-08).

**`nlc_slot_sel`.** Counts slots (restart on `s_frame`), emits the configured slots as channels
0..n_sel-1 with first/last-of-frame flags, starting at the first `s_frame` after enable.
Frame rule (D5): `smp_tick` marks each new frame (from `s_frame`), `smp_short` flags that the
frame before it did not reach all selected slots; the encoder then aborts the packet (D6 path).

**`nlc_lossy` (inside `nlc_encoder`, which is a lossy-only wrapper, D1).**
1. *Wavelet:* 3 levels of LeGall 5/3 lifting, streaming, 64-sample blocks, on **one shared
   lifter** (`nlc_lift53`, 12 bit) driven by the issuer: a sample is only stored on arrival
   (10 b per channel); the issuer lifts it one level per symbol it issues (kk = 0/1/2: level
   1/2/3 -> d1/d2/d3, a 13-bit register carries a1/a2 between levels and then delta a3 for
   kk = 3). A level whose pair is not complete only stores its input (level 1 in the
   empty-burst frames, one clock per channel). Per-channel state = 3 registers per level, in
   rows indexed by channel. All channels share one block position, so the schedule is global.
   Issue order and cycle timing are those of the former burst buffer (area experiment B,
   2026-10-09: -2 lifters, -32 b/ch of burst buffer).
2. *Quantise:* right shifts d1 >> 3, d2 >> 2, d3 >> 2, a3 >> 1; a3 is delta-coded. Explicit
   bit slices, no signed casts (C-RTL-2).
3. *Issuer:* bursts of 0/1/2/4 symbols per sample (d1, d2, d3, delta a3; position =
   context), taken as soon as the sample is there, frame by frame, channel by channel
   (`nlc.lossy.coding_order`, the bitstream order); a frame needs at most 4 x 8 symbols
   against 256 clocks, so nothing lags. The coder may lag up to a frame; more aborts (the
   channel's next sample would overwrite the stored one).
4. *rANS (`nlc_rans`):* one 22-bit state per channel, one symbol per clock, 2 stages:
   state read + ROM, then renorm + a 10-step combinational divider + write-back (`DIV_REG`
   can put registers back between the steps; the old 13-stage pipeline cost 547 more flops). Static tables in a synthesised ROM (`nlc_lossy_rom.v`):
   4 contexts, 64 symbols + escape (escape = 2 raw bytes). 0-4 bytes per symbol. Packet end:
   flush all 8 states, 3 bytes each.
5. *Serialiser:* header `{mode = 1, seq[5:0]}`, then the coder bytes read in place from the
   coder's output word (no copy), `m_last` on the last.

A packet = 4 blocks = 256 frames (13.1 ms), ~650 bytes on real data, up to ~5,500 on noise.
Packets decode independently. Format spec: docstrings of `model/nlc/lossy.py`, `rans_tdm.py`.

**Clock gating (`nlc_icg.sv`).** Every register that is not needed every cycle is behind
a sky130 `dlclkp` integrated clock gate, in two levels. Parent gates open only when a block
has work: `u_cg_s` (a sample, 8 of 256 clocks), `u_cg_i` (sample or pop), `u_cg_o`
(serialiser), `u_rans.u_cg_c` (coder busy). Under them, `nlc_greg` registers (one gate +
plain `dfxtp`, no enable mux) hold each channel's wavelet fields, each FIFO slot, each coder
state row, stage A, the output word and the serialiser buffer. Control registers clear
asynchronously on `clr_n` (`rst_n && enable`), so a disabled core sees no clock edge.
RTL simulation models `nlc_greg` as an enable flop (same behaviour, faster in Icarus);
synthesis and gate-level use the real cell (`+define+NLC_ICG_SIM` simulates the gate in RTL).
Only `overflow` runs on `clk` in the core. Outside it: `nlc_cfg` (one gate, opens on a config
write), `nlc_out_fifo` (a gate per entry, one for the pointers), `nlc_slot_sel` (async clear
on `rst_n && enable`, counters gated on slots while enabled, sample registers on a hit),
`project.v` (config address/data gated). Clocked every cycle: the TT pin registers and
the few flops that must run (`slot_sel` counters while enabled).

**Abort (D5/D6/D7).** `nlc_lossy` aborts the packet in flight when the coder falls more than
a frame behind (blocked output: the next burst would overwrite one not yet coded) or on a
short frame: issuer, coder and serialiser clear, samples are ignored while frames keep
counting, and output resumes at the next packet start at which the FIFO takes bytes (its
header carries that packet's seq, so the gap names the lost packets). The encoder ends a
packet the host holds partly with the **abort token** (also when `enable` falls; its token
logic is reset by `rst_n` only). On the TT pins: `m_last` = 1 with `m_valid` = 0, acked like a
byte.

**`nlc_out_fifo`.** 8 entries of {abort, last, byte}, valid/ready; `m_abort` with the head.

## Cost today

| | value |
|---|---|
| area | 59,200 um^2 lossy core (was 128,000), 66,000 um^2 `nlc_core`; 23% of an 8x2 TT design |
| flip-flops | 1,573 + 127 clock gates in the lossy core (was 2,681) |
| state per channel | ~146 b: 114 wavelet + 10 sample + 22 coder (Neuralink spike path: 226 b/ch) |
| timing | 122 ns slack at ss / 200 ns (TT top); deepest path ~117 cells (TT unit-delay gate sim needs < 200) |
| power | `nlc_core` (system): 12.3 uW op, 0.93 uW idle. Lossy core: 8.1 uW op, 8.7 worst, 0.16 idle (was 629 uW; budget 40/16 uW, idle 10/2) |

Remaining levers (2026-10-08, docs/results.md): latch-based storage for the gated rows
(~-8k um^2, same-cycle read/write hazards); the read muxes (8:1 x 114 b wavelet state, 32:1 x
13 b bursts, ~9k um^2) only go away with rotating storage, which costs ~+6.5 uW; register
widths are already at the filter bounds; removing coder stage A saves 64 flops but no area.

## Where to change what

| change | files | tests that must stay green |
|---|---|---|
| algorithm (shifts, block, tables) | `model/nlc/lossy.py`, then RTL constants, ROM via `scripts/gen_lossy_rom.py` | `pytest` (change guard), `test/lossy`, `test/core` |
| D5 frame rule | `smp_tick`/`smp_short` in `nlc_slot_sel.v`, abort in `nlc_lossy.sv` | T-ROB-4 |
| D6/D7 abort, resume, token | abort/skip/resume in `nlc_lossy.sv`, token in `nlc_encoder.v`, `nlc_out_fifo.v`, `m_abort` on `nlc_core`, pins in `project.v` | T-OVF-1/2, T-IF-3b, T-ROB-2/3/7 |
| power (clock gating) | `nlc_icg.sv`; parent gates and `nlc_greg` rows in `nlc_lossy.sv`, `nlc_rans.sv` | everything + `nlc.py power` (fails if an activity annotation is lost) |
