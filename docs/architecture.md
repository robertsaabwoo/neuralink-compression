# Architecture (as built, 2026-10-10)

What the RTL does today (branch `area4-final-3x2`, RTL a0966c0). Requirements:
[constraints.md](constraints.md) (decisions D1-D10). Numbers: [results.md](results.md).

```
 TT pins ─► project.v ─► nlc_core ──────────────────────────────────────────────► TT pins
          (pin protocol,  ├─ nlc_cfg       config registers (+ readback mux)
           CDC, output    ├─ nlc_slot_sel  256-slot ADC stream -> up to 8 channels
           register)      └─ nlc_encoder   wrapper; mode 1 = nlc_lossy:
                               sample row -> shared lifter -> quantise -> rANS -> header + bytes
                               (valid-only byte stream, no back-pressure: D8)
```

One clock, 5 MHz = the ADC slot rate (D2). The system is II = 1 (D9): at most one sample per
clock, the ADC is never stalled, and the output consumer takes every byte.

## Blocks

**`project.v` (TT top).** Pin CDC: `rst_n` goes through a 2-flop reset synchroniser (async
assert, sync release; the synchronised reset is the only reset inside the chip); `s_strobe`
and `cfg_en` through 2-flop synchronisers, the strobe edge-detected on stage 2/3; `s_frame`
sampled with strobe stage 1; the sample bus loaded once, one clock after strobe stage 1, while
the host still holds it, by a clock gate whose enable is built from flop outputs only (no raw
pin reaches a gate enable; the stage-1 MTBF argument is in `project.v`). A strobe carries one
10-bit sample (`ui_in` + `uio[1:0]`, `s_frame` = slot 0), or a config byte (address, then
data) while `cfg_en` = 1. Output: byte on `uo_out`, `m_valid`/`m_last` on `uio[6]/[7]`, all
from one output register stage (+1 clock; D8: no ack). Test access: sticky `overflow` on
`uio[4]`; config readback on `uo_out` while `cfg_en` = 1 and `CTRL.enable` = 0 (C-IF-12). The
pin path manages one sample per 2 clocks at most: a test-access mode below the D9 rate (C-IF-9).

**`nlc_cfg`.** Registers: enable (CTRL[7]; the mode bits CTRL[1:0] are ignored, lossy only),
`n_sel` (1-8), slot per channel (ascending), and a read mux for the readback. `enable` = 0
clears the core. The slot registers are gated storage without reset, read through one
shared slot mux. The registers of the model-only modes 0/2/3 were removed (2026-10-08).

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
   empty-burst frames, one clock per channel). Per-channel state = the sample, 3 registers
   per level and the previous quantised a3, in rows indexed by channel: **latch rows**
   (`nlc_lreg`, `LATCH_ROWS` = 1), written from shared staging flops so a latch never feeds
   its own input (results.md F22, F24). All channels share one block position, so the schedule is global.
   Issue order and cycle timing are those of the former burst buffer (area experiment B,
   2026-10-09: -2 lifters, -32 b/ch of burst buffer).
2. *Quantise:* right shifts d1 >> 3, d2 >> 2, d3 >> 2, a3 >> 1; a3 is delta-coded. Explicit
   bit slices, no signed casts (C-RTL-2).
3. *Issuer:* bursts of 0/1/2/4 symbols per sample (d1, d2, d3, delta a3; position =
   context), taken as soon as the sample is there, frame by frame, channel by channel
   (`nlc.lossy.coding_order`, the bitstream order); a frame needs at most 4 x 8 symbols
   against 256 clocks, so nothing lags. The coder may lag up to a frame; more aborts (the
   channel's next sample would overwrite the stored one). At the D9 rate this cannot happen
   for any data (C-IF-9: the bound is the 1 byte/clock output, results.md F25).
4. *rANS (`nlc_rans`):* one 22-bit state per channel (latch rows). The 10-step divider is
   looped: `DIV_K` = 5 quotient bits per clock, so a symbol takes 2 clocks; the issuer holds
   its symbol (valid/ready) until the coder takes it. 32 symbols x 2 clocks fit a 256-clock
   frame; the throughput proof gives the same slack as a single-cycle divider (F25). Static
   tables in a synthesised ROM (`nlc_lossy_rom.v`):
   4 contexts, 64 symbols + escape (escape = 2 raw bytes). 0-4 bytes per symbol. Packet end:
   flush all 8 states, 3 bytes each.
5. *Serialiser:* header `{mode = 1, seq[5:0]}`, then the coder bytes read in place from the
   coder's output word (no copy), `m_last` on the last.

A packet = 4 blocks = 256 frames (13.1 ms), ~650 bytes on real data, up to ~5,500 on noise.
Packets decode independently. Format spec: docstrings of `model/nlc/lossy.py`, `rans_tdm.py`.

**Clock gating (`nlc_icg.sv`).** Every register that is not needed every cycle is behind
a sky130 `dlclkp` integrated clock gate, in two levels. Parent gates open only when a block
has work: `u_cg_s` (a sample, 8 of 256 clocks), `u_cg_i` (sample or pop), `u_cg_o`
(serialiser), `u_rans.u_cg_c` (coder busy). Under them, each channel's wavelet fields and
coder state are `nlc_rreg` rows (one gate + a latch row, `nlc_lreg`; or plain `dfxtp`
flops, `nlc_greg`, when `LATCH_ROWS` = 0), and `nlc_greg` holds the staging registers, the
output word and the serialiser buffer. The gate of a row (`g_ch[i].u_*.u_icg`) hangs off a
parent clock such as `clk_i`. Control registers clear asynchronously on `clr_n`
(`rst_n && enable`), so a disabled core sees no clock edge.
RTL simulation models `nlc_greg` as an enable flop (same behaviour, faster in Icarus);
synthesis and gate-level use the real cell (`+define+NLC_ICG_SIM` simulates the gate in RTL).
Only `overflow` runs on `clk` in the core. Outside it: `nlc_cfg` (one gate, opens on a config
write), `nlc_slot_sel` (async clear on `rst_n && enable`, counters gated on slots while
enabled, sample registers on a hit), `project.v` (sample/config loads gated on selected
strobes). Clocked every cycle: the pin synchronisers, the output register and the few flops
that must run (`slot_sel` counters while enabled). The pin registers hang off their own
always-on gate and one gate sits in front of the whole core, so CTS has no nets mixing
gates and flops to balance (results.md F19, F20).

**Abort (D5/D6/D7).** `nlc_lossy` aborts the packet in flight on a short frame, or when the
coder falls more than a frame behind (the next burst would overwrite one not yet coded; since
D8 only possible below the D9 rate; it sets the sticky `overflow`): issuer, coder and
serialiser clear, samples are ignored while frames keep counting, and output resumes at the
next packet start (its header carries that packet's seq, so the gap names the lost
packets). The encoder ends a packet the consumer holds partly with the **abort token** (also
when `enable` falls; its token logic is cleared by reset only). On the TT pins: `m_last` = 1
with `m_valid` = 0 for one clock.

**Output (D8).** No output FIFO: the serialiser drives the byte stream directly, one byte per
clock while `m_valid`, `m_abort` for one clock. The TT top registers it once.

## Cost today

| | value |
|---|---|
| area | TT top 48,518 um^2 synth (the lossy core alone was 128,000 um^2 on 2026-10-07); 3x2 tiles, post-CTS utilisation 0.553 (D10) |
| storage | 337 flops + per-channel latch rows + 126 clock gates (TT top) |
| state per channel | 146 b: 124 sample/wavelet + 22 coder (Neuralink spike path: 226 b/ch) |
| timing | +129.0 ns setup slack at ss / 200 ns, +0.187 ns hold at ff (pre-layout); latch D pins excluded from place-and-route setup repair (`src/pnr.sdc`, results.md F24) |
| power | routed, real data, signed off (e13 RTL, before the area round): 34.8 uW running / 9.5 uW idle for the TT top, core 23.8 / 0.80. Final RTL: preview only, synthetic data (results.md, Budgets) |

Open: the OpenROAD CTS bug that leaves one `g_ch[7]` clock gate without CLK (results.md
F27); the definition of processing latency (F1).

## Where to change what

| change | files | tests that must stay green |
|---|---|---|
| algorithm (shifts, block, tables) | `model/nlc/lossy.py`, then RTL constants, ROM via `scripts/gen_lossy_rom.py` | `pytest` (change guard), `test/lossy`, `test/core` |
| D5 frame rule | `smp_tick`/`smp_short` in `nlc_slot_sel.v`, abort in `nlc_lossy.sv` | T-ROB-4 |
| D6/D7 abort, resume, token | abort/skip/resume in `nlc_lossy.sv`, token in `nlc_encoder.v`, `m_abort` on `nlc_core`, pins in `project.v` | T-IF-3, T-ROB-2/3/4/7 |
| throughput (D9) | coder speed `DIV_K` in `nlc_lossy.sv` / `nlc_rans.sv`; output rate in the serialiser | T-BW-3, `scripts/proofs/output_bound.py` |
| pins, CDC, test access | `project.v` (synchronisers, output register, overflow pin), readback mux in `nlc_cfg.v` | `test/` (T-IF-4/6/7), STA recovery |
| place and route | `src/config.json` (LibreLane), `src/pnr.sdc` (latch D pins out of setup repair), `info.yaml` tiles | `cts_preview`, `gds` on GitHub |
| power (clock gating) | `nlc_icg.sv`; parent gates and `nlc_greg` rows in `nlc_lossy.sv`, `nlc_rans.sv` | everything + `nlc.py power` (fails if an activity annotation is lost) |
