# Architecture (as built: commit 921e1e0, "e13", signed off 2026-10-09)

What the RTL on this branch does. Requirements: [constraints.md](constraints.md). Numbers:
[results.md](results.md). Decisions D5-D7 (frame rule, abort, reset) are implemented. Newer
area work (shared lifter, looped divider, latch rows, valid-only output D8) lives on the
`area3-*` / `area4-*` branches and is not described here.

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

**`project.v` (TT top).** Registers all inputs once (single flop, no synchroniser: see README
known limitations), in three gated clock groups (F19/F20): the strobe, ack and `cfg_en` levels
and edge detectors on an always-on gate; `s_frame` on strobes; the sample/config byte only on a
strobe of a selected slot (`s_want` from the slot selector), a frame strobe or a config strobe.
`s_strobe` and `m_ack` are edge-detected into one-clock pulses. A strobe carries one 10-bit sample (`ui_in` + `uio[1:0]`, `s_frame` =
slot 0), or a config byte (address, then data) while `cfg_en` = 1. Output: byte on `uo_out`,
`m_valid`/`m_last` on `uio[6]/[7]`, host pulses `m_ack`. The pin path manages one sample per 2
clocks at most: it is for slow real-data tests (C-IF-9).

**`nlc_cfg`.** Registers: enable (CTRL[7]; the mode bits CTRL[1:0] are ignored, lossy only),
`n_sel` (1-8), slot per channel (ascending). `enable` = 0 clears the core. The registers of
the model-only modes 0/2/3 were removed from the RTL (2026-10-08).

**`nlc_slot_sel`.** Counts slots (restart on `s_frame`), emits the configured slots as channels
0..n_sel-1 with first/last-of-frame flags, starting at the first `s_frame` after enable.
Frame rule (D5): `smp_tick` marks each new frame (from `s_frame`), `smp_short` flags that the
frame before it did not reach all selected slots; the encoder then aborts the packet (D6 path).

**`nlc_lossy` (inside `nlc_encoder`, which is a lossy-only wrapper, D1).**
1. *Wavelet:* 3 levels of LeGall 5/3 lifting (`nlc_lift53` x3), streaming, 64-sample blocks.
   One shared combinational datapath; per-channel state = 3 registers per level, in rows
   indexed by channel (time-multiplexed). All channels share one block position, so the
   schedule is global.
2. *Quantise:* right shifts d1 >> 3, d2 >> 2, d3 >> 2, a3 >> 1; a3 is delta-coded. Explicit
   bit slices, no signed casts (C-RTL-2).
3. *Per-channel burst buffer*, 4 slots of 8/10/11/13 bits per channel (the lifting's value bounds): a sample pushes 0/1/2/4 symbols
   (d1, d2, d3, delta a3) into fixed slots (slot = context). The issuer drains every burst at
   once, frame by frame, channel by channel (`nlc.lossy.coding_order`, the bitstream order);
   a frame needs at most 4 x 8 symbols against 256 clocks, so nothing lags (was: 8-deep
   FIFO, one pop per sample, peak 7, 6-frame lag).
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
Since e13 one more gate, `u_cg_core` in `nlc_core`, sits in front of the whole core: it is open
only while enabled, on a config write, while the FIFO holds bytes, or while the encoder needs
its disable edge / abort token (idle 0.80 uW core share, F20). Only `overflow` runs on the core
clock without a block gate. Outside the lossy core: `nlc_cfg` (one gate, opens on a config
write), `nlc_out_fifo` (a gate per entry, one for the pointers), `nlc_slot_sel` (async clear
on `rst_n && enable`, counters gated on slots while enabled, sample registers on a hit),
`project.v` (pin registers in three gated groups, above). Clocked on every core clock: the
few flops that must run (`slot_sel` counters while enabled).

**Abort (D5/D6/D7).** `nlc_lossy` aborts the packet in flight when the coder falls more than
a frame behind (blocked output: the next burst would overwrite one not yet coded) or on a
short frame: issuer, coder and serialiser clear, samples are ignored while frames keep
counting, and output resumes at the next packet start at which the FIFO takes bytes (its
header carries that packet's seq, so the gap names the lost packets). The encoder ends a
packet the host holds partly with the **abort token** (also when `enable` falls; its token
logic is reset by `rst_n` only). On the TT pins: `m_last` = 1 with `m_valid` = 0, acked like a
byte.

**`nlc_out_fifo`.** 8 entries of {abort, last, byte}, valid/ready; `m_abort` with the head.

## Cost (e13)

Synthesis: local check flow (`reports/*/lossy_stat.txt`, `top_stat.txt`); the lossy core RTL is
unchanged from `a9c2329`. Routed: LibreLane metrics of gds run 37865677796.

| | value |
|---|---|
| area, synthesised | 60,050 um^2 lossy core (was 128,000 before clock gating), 67,100 um^2 `nlc_core`, ~68,200 um^2 TT top |
| area, routed | 102,600 um^2 standard cells (logic 43.3k, flops 39.1k, repair + hold buffers 11.7k, clock buffers 2.5k, clock gates 2.6k), utilisation 0.69 of the 4x2 core (149,200 um^2) |
| flip-flops | 1,582 + 127 clock gates in the lossy core (was 2,681) |
| state per channel | ~178 b: 114 wavelet + 42 burst buffer + 22 coder (Neuralink spike path: 226 b/ch) |
| timing | routed: +118.5 ns setup at ss / 200 ns, +0.187 ns hold at ff. Pre-layout deepest path ~117 cells (TT unit-delay gate sim needs < 200) |
| power | routed, real data, whole TT design: 34.8 uW op, 9.5 uW idle (core share 23.8 / 0.80), patched CTS image (results.md F20). Pre-layout: `nlc_core` 12.5 / 1.07 uW, lossy core 8.3 uW op, 0.16 idle (was 629 uW; budget 40/16 uW, idle 10/2) |

Remaining levers (2026-10-08, docs/results.md; several are now being tried on the `area3-*` /
`area4-*` branches): latch-based storage for the gated rows
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
