# Summary: lossy neural-signal compressor on Tiny Tapeout (sky130)

Robert Saab, October 2026. Design shown: commit `921e1e0` ("e13"), the signed-off build.
Sign-off: [gds run 37865677796](https://github.com/robertsaabwoo/neuralink-compression/actions/runs/37865677796)
(gds, gl_test and precheck green; the `viewer` job fails only because GitHub Pages is off).

## Problem

A Neuralink-style implant digitises 256 electrodes per ASIC through one multiplexed 10-bit ADC,
one slot every ~200 ns. The radio carries ~1 Mbit/s for 1024 channels and the implant gets
< 10 mW including the radio, so only a few raw-waveform channels fit and the compressor gets a
few uW per channel. The compression patent (US 12,369,863 B2) describes a lossy mode for 4-8
channels: wavelet plus entropy coding. Goal: build that mode for 8 of 256 slots and measure its
real silicon cost (area, power, timing after layout), not to win on compression ratio.

## Approach

- **Golden model first** (`model/nlc`, Python): the bit-exact spec. Every RTL and gate-level test
  compares bytes with it and decodes the result.
- **Datapath:** 3-level LeGall 5/3 integer wavelet on 64-sample blocks (streaming), quantiser,
  static rANS (one 22-bit state per channel, ROM tables). One shared datapath, time-multiplexed
  over the channels at the ADC slot rate: 5 MHz, one slot per clock, the ADC is never stalled.
- **Requirements as observable constraints** (`C-*`, `docs/constraints.md`), each traced to tests
  (`T-*`, `docs/testing.md`) and to numeric budgets (`docs/budgets.md`).
- **Post-layout loop:** CI hardens each variant with TT's flow; a local step re-simulates the
  routed netlist on real recordings and runs OpenSTA power on that activity.

## Key numbers (e13 unless stated)

| metric | value | source |
|---|---|---|
| compression | 2.087 bits/sample (~4.8:1 on 10-bit samples), 18.0 dB median SNR, 160 held-out recordings | `compress` step (T-ALG-2) |
| power, routed, real data | **34.8 uW running / 9.5 uW idle** for the whole TT design (pins, clock tree); compression core share 23.8 / 0.80 uW (~3.0 uW/channel) | T-PWR-3 on run 37865677796; results.md F20 |
| power caveats | built with a patched LibreLane image (CTS latency balancing off, not TT-standard). Same RTL on TT's standard flow: 51.5 / 20.8 uW. Best TT-standard variant: 40.7 / 17.8 uW. Activity from unit-delay simulation | local re-measurements of CI artifacts |
| area | lossy core 60.1k um^2 synthesised (128k before clock gating), TT top ~68k; routed: 102.6k um^2 standard cells, 11.7k of them repair/hold buffers, utilisation 0.69 of a 4x2 tile | Yosys stats; LibreLane metrics of run 37865677796 |
| timing (routed) | setup +118.5 ns at 200 ns (ss), hold +0.187 ns (ff) | LibreLane, run 37865677796 |
| latency | delivery 13.1 ms (target 20); **processing 1,846 us vs 1 ms: FAIL** (definition open, F1) | T-LAT-1 |

## Engineering story

1. **628 -> 9.5 uW before layout.** The first RTL drew 629 uW, 98% of it flip-flop clock pins:
   each channel's state changes once per 256 clocks. Two-level clock gating (a sky130 `dlclkp`
   per channel row, parent gates per block) brought the lossy core to 9.5 uW (F11).
2. **The routed chip drew 80 uW, not ~17.** Measuring the routed netlist on real data put the clock
   tree at 48 of 80 uW (F17). Root cause: OpenROAD's CTS latency balancer treats each clock gate
   as a sink with insertion delay and pads the shallow always-on branch (the TT pin registers)
   with delay buffers (F19). Fixes, each routed and measured: smaller clock buffers, pin
   registers behind their own gate, CTS without latency balancing, pin data loaded only on
   selected slots, one gate in front of the core: **80.0 -> 34.8 uW running, 51.4 -> 9.5 idle** (F20).
3. **Flow overhead.** Synthesis to routed adds ~46% cells on e13 (68k -> 99.6k excluding taps):
   repair/hold buffers on high-fanout selects and clock buffers (F18). The area branches attack
   this together with the RTL.
4. **Throughput proof (area branch).** A max-plus model of issuer, coder and output, with every
   symbol at its 4-byte worst case: the valid-only streaming output (no host handshake) keeps
   +138 clocks of slack at the real 256-clock frame, +10 at 128-clock frames and -118 at the
   64-clock test frames. Short test frames can therefore abort; the seq gap detects it.
5. **Latch rows vs LibreLane (area branch).** Latch-based channel storage hung post-CTS repair: all
   1,168 latch D pins had setup slack exactly 0 (time borrowing), and the resizer chased them
   indefinitely. A P&R-only SDC with a setup false path to those pins (sign-off SDC unchanged)
   fixed it: LibreLane now reaches the end of post-CTS repair in ~3 minutes.

## Status and next steps

- **Signed off:** e13. **Work in progress, not signed off:** `area4-stack` (shared lifter, looped
  divider, latch rows, valid-only output, TT 2x2): 47.5k um^2 synthesised, post-CTS utilisation
  0.78 with +0.012 ns hold; its gds run and full regression are pending.
- **Open gaps:** processing-latency definition (F1); unit-delay-only gate-level simulation, no SDF
  (F21); single-flop pin capture, raw pins into clock-gate enables and unsynchronised reset
  release (fix in progress on `area4-stack-cdc`); no scan/DFT; power 2x over the 16 uW goal.
- **Next:** SDF gate-level simulation; synchronisers; sign off the 2x2 stack; for 256 channels,
  per-channel state in single-port SRAM rows, as Neuralink's spike-detection patent does.

Independent learning project; not affiliated with or endorsed by Neuralink.
