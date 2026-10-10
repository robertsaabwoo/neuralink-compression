# Summary: lossy neural-data compression on a Tiny Tapeout chip

One page for a reviewer. Every number has a source (CI run, report or finding in
[results.md](results.md)); caveats sit next to the number.

## What it is

An implant digitises its electrodes on a time-multiplexed 10-bit ADC (256 slots, one per clock,
5 MHz, 19.5 kHz per channel). This chip compresses up to 8 channels picked from the 256 slots
with the lossy scheme of Neuralink's compression patent (US20230284982A1, "between 4 and 8
channels" over Bluetooth): LeGall 5/3 integer wavelet (bior2.2), 3 levels, quantisation, static
rANS. It is the broadband monitoring mode, not the spike-count stream that drives BCI control,
and it is lossy: it does not answer the lossless compression challenge. The goal is to measure
what this mode costs in silicon (sky130 130 nm, 1.8 V).

```
ADC slots -> slot selector -> shared 5/3 lifter (3 levels) -> quantiser -> issuer
          -> TDM rANS coder (ROM tables, 22-bit state per channel) -> bytes, up to 1 per clock
```
Design: [architecture.md](architecture.md). Requirements and decisions: [constraints.md](constraints.md).

## Results (main; signed-off layout: gds run 38060355759, 3x2 tiles)

| | result | caveat |
|---|---|---|
| compression | 2.09 bits/sample (4.8:1), median SNR 18.0 dB (10th percentile 13.3 dB), 160 held-out recordings | SNR, not spike-detection agreement; RTL bit-exact with the golden model |
| throughput | II = 1: one slot per clock in every state; no input can make it drop a packet at 256-clock frames (proof: +138 clocks of slack, `scripts/proofs/output_bound.py --d8`; RTL test T-BW-3 on worst-case data) | output is not rate-capped: noise-like data codes at 21.4 bits/sample, above the raw rate; real data 2.1 |
| power, routed, real data | **24.5 uW** running / **0.6 uW** idle for the compression core; **26.7 / 2.8 uW** with its clock root (the in-scope block, table below) | patched clock-tree step (no latency balancing, F19); TT's standard image: 49.0 / 17.1 uW for the whole TT chip (post-CTS preview, synthetic data) |
| area | 48.5k um^2 of cells after synthesis (337 flops, 1,168 latch bits, 126 clock gates); routed: 62.5k um^2 incl. clock tree and repair buffers, utilisation 0.56 of a 3x2 tile area | was 128k; 8 channels do not route in a 2x2 (F23) |
| timing | setup +126.8 ns at 200 ns, hold +0.20 ns (routed, sign-off corners) | |
| sign-off | gds + precheck + gate-level test green (run 38060355759); SDF gate level bit-exact at 5 corners with 0 timing-check violations (F28) | |
| latency | sample -> packet decodable 13.4 ms; wavelet look-ahead 1.85 ms (F1) | fine for monitoring, not for a control loop |

### Power breakdown (routed, real data, gds run 38060355759)

**Scope.** The deliverable is the compression block as it would sit on an implant SoC, next to
the ADC and the radio: the core plus its clock root. The Tiny Tapeout pin interface exists only
to feed a test chip through slow GPIO pins (2-flop input synchronisers, registered outputs, the
strobe protocol that emulates the ADC's slot stream at one slot per two clocks). On an implant
the ADC and radio logic connect on chip, so that interface is out of scope; it is listed so the
chip total adds up.

| block | running | idle | in scope |
|---|---|---|---|
| compression core (slot selector, lifter, quantiser, rANS coder, config) | 24.5 uW | 0.6 uW | yes |
| clock root (trunk to the core's clock gates) | 2.2 uW | 2.2 uW | yes |
| **in-scope total** | **26.7 uW** | **2.8 uW** | budget 40 / 10 uW: pass |
| TT pin interface (synchronisers, registered outputs, strobe protocol) | 9.8 uW | 6.4 uW | no: TT test access |
| other (tie cells, glue outside both groups) | 3.7 uW | 1.0 uW | partly |
| whole TT chip | 40.3 uW | 10.2 uW | |

Running = 8 channels of real recordings at 5 MHz; idle = configured but disabled while the
ADC stream keeps arriving (the state between recordings). Per
channel in scope: 3.3 uW at 130 nm and 1.8 V. Block attribution by net names in the routed
netlist (`scripts/flow/netlist.py`).

Power history (routed, real data): 80.0 uW -> 34.8 uW (e13) by gating the clock tree (F17-F20),
then +5 uW for the CDC fixes and test pins on the TT side (F26). Pre-layout the core went
628.5 -> 9.5 uW with clock gating (F10-F12).

## Verification

| level | what | data | status |
|---|---|---|---|
| model | golden codec, round trip, packet format, change guards | real recordings | 80 tests pass |
| RTL, core | interface, slot sets, faults (short frame, disable, reset), random power-up, bandwidth, II = 1 worst case | real, synthetic, LFSR, edge patterns | 46/46 |
| RTL, TT top | config, readback, overflow pin, bit-exact through the pins, abort token at the pins | real / synthetic | 7/7 |
| gate level | synthesised netlist (unit delay): TT top incl. abort paths, lossy core | | pass |
| routed | routed netlist, real data through the pins, power from its activity | real | bit-exact |
| SDF | routed netlist with back-annotated delays and timing checks, tt/ss/ff and min/max | synthetic | 5 corners pass, 0 violations |
| bring-up | diagnosis tool: output differences -> faulty block / bit, validated by RTL fault injection ([bringup.md](bringup.md)) | real | 26/26 faults diagnosed |

## Test access

Reset synchroniser and 2-flop input synchronisers; sticky overflow flag on `uio[4]`; config
readback on `uo_out`; the bring-up diagnosis tool. Raw-bypass and clock-gate-observation debug
modes exist on branch `area4-final-dft` (+3% area), not in this build: OpenROAD's global router
crashed in post-route repair on that layout (GRT-0183), routing fixes being signed off. No scan chain (TT has no scan flow).

## Known limitations

- The in-scope block meets 40 / 10 uW with the patched clock-tree step; with TT's standard
  image the whole TT chip is 49 / 17 uW (the in-scope share was not split for that run).
- No rate cap: worst-case data exceeds a 1 Mbit/s radio; real data uses 0.33 Mbit/s.
- rANS tables and quantiser shifts are fixed in ROM (trained on one dataset).
- Fidelity is reported as SNR; spike-detection agreement is not measured yet.
- Real recordings are not public: CI runs synthetic data; real-data numbers are local runs.
- Per-channel state in flops and latches: fine at 8 channels; more would need SRAM (TT sky130
  has none).

## Next

1. Rate-bounded output: raw escape per block, quantiser rate control against a packet budget.
2. Spike-band power or threshold-crossing counts for all 256 slots (25 ms bins) from the same
   filter bank: the data Neuralink's control path uses.
3. Loadable rANS tables and quantiser shifts.
4. Fidelity as spike-detection agreement on held-out recordings.
5. SRAM-backed channel state; power at a low-voltage modern node.
