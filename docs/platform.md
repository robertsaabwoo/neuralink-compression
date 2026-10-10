# Platform: what Neuralink's system looks like, and what Tiny Tapeout allows

Reference for the testing and design phases. **Quoted** facts carry a source
number (list at the end); **derived** numbers are marked as such, with the
arithmetic shown. Where no source says something, this document says so and
lists it under "Not specified".

Order of work agreed for the project: (1) understand the platform (this file),
(2) build good testing, (3) design. RTL before step 3 is limited to testbenches
and peripherals (e.g. the input generator in section 4.4).

---

## 1. Neuralink system level (N1 implant)

| | value | source |
|---|---|---|
| Electrodes | 1024, on 64 threads (later implants: 128 thinner threads) | [5] |
| Raw data | "~200Mbps of electrode data (1024 electrodes @ 20kHz, 10b resolution)" | [3] |
| Radio | "can transmit ~1Mbps wirelessly", Bluetooth | [3], [5] |
| Required compression | "> 200x" | [3] |
| Latency | "real time (< 1ms)" | [3] |
| Power | "< 10mW, including radio" | [3] |
| Supply | small battery, charged inductively | [5] |

**Derived budget per channel** (1024 channels):
- power for compression + radio: 10 mW / 1024 = **~9.8 uW/channel** (the README's single
  target; it does not cover the analog front end/ADC, ~6 mW per 256 channels in 2019 [4],
  and gives no split between compression and radio);
- radio share: 1 Mbps / 1024 = **~980 bit/s/channel on average**, i.e. ~0.05 bit/sample at
  19.5 kS/s. Raw-waveform compression (lossless ~3:1, lossy ~6:1 [2]) is far from this;
  only spike modes get there, which is consistent with the patent sending raw/lossy data for
  a few channels only (section 3).

**Unverified number in this repo:** `model/nlc/energy.py` uses ~10 nJ/bit for the
radio. No source found. It is also inconsistent with [3]: 1 Mbps x 10 nJ/bit = 10 mW, the
entire budget for compression *and* radio. Treat it as an assumption until sourced.

## 2. The ASIC and its on-chip datapath

### 2.1 Analog front end and ADC (2019 paper [4])
- "256 individually programmable amplifiers" per ASIC; gain 42.9-59.4 dB; bandwidth 3-27 kHz;
  input-referred noise 5.9 uVrms; **5.2 uW per channel**; whole ASIC **~6 mW**.
- On-chip ADCs, **10-bit, 19.3 kHz** (system B: 18.6 kHz), with "peripheral control circuitry
  for serializing" the data.
- Systems: 1536 channels = 6 ASICs (550 mW system), 3072 channels = 12 ASICs (750 mW). In
  2019 spike detection ran off-chip (FPGA), data left over USB-C.
- N1 has 1024 channels; four 256-channel ASICs would match, but no source confirms the N1
  ASIC count. (not specified)

### 2.2 Digital datapath (spike-detection patent [1])
- Input: "a 256 channel multiplexed ADC"; "10 bit (b) samples at a sampling rate of
  approximately 16-20 KHz".
- Processing is **time-multiplexed over all channels**: the detector "operates on a sample by
  sample basis, receiving inputs from each of a plurality of channels on the chip in
  succession".
- **Per-channel state in single-port SRAM ring buffers**, one row per channel:
  band-pass filter 32 b/channel, MAD/threshold 22 b/channel, spike fitting (BOS) 172 b/channel
  ("a single-port 172 x 256 SRAM ring buffer"). Total 226 b/channel, 256 rows.
- Spike decision in "roughly 1 microsecond" from the sample.
- The compression engine "may discard or otherwise refrain from transmitting a signal"; it can
  "packetize and transmit" anything from timestamps only up to the raw voltage signal.
- "The merge circuitry 228 sends packets off chip through a serializer 232 out to the next
  chip or to an external computing device": chips are daisy-chained ("a chip network").
- Power example in the patent: "1-2 milliwatts (mW) per channel or less. For 256 channels,
  the total system power consumption is under 50 milliwatts".

**Derived timing per ASIC:**
- slot rate = 256 x 19.3 kHz = **4.94 MS/s**, i.e. one sample every **202 ns**;
- each channel comes back every 256 slots = **51.8 us**;
- a datapath that keeps up handles **one sample per slot**, so its clock must be at least the
  slot rate (~5 MHz); faster clocks give it more cycles per sample. The actual chip clock is
  not published.

## 3. Compression (compression patent [2])

- Runs on the implant chip: "the method is executed on an integrated circuit communicatively
  coupled to the plurality of electrodes", then "transmitting ... off of the integrated
  circuit via a short-range wireless communication".
- Framing: "collecting a frame of 10 or more samples at a sampling rate of between 100 to
  1,000 samples per window of 10 milliseconds to 40 milliseconds"; example "20,000 samples
  per second, or 500 samples in 20 milliseconds".
- Modes, channel counts and ratios (over Bluetooth):

| mode | method in the patent | channels sent | ratio |
|---|---|---|---|
| lossless (broadband) | delta + Rice-Golomb | "between 2 and 4 channels simultaneously" | ~3:1 |
| lossy (broadband) | bior2.2 DWT + quantisation + rANS | "between 4 and 8 channels simultaneously" | ~6:1 |
| binned spikes | spike counts per bin + rANS | (all channels) | 8:1, or 23:1 with spike categories |
| spike-band power | thresholded power + bit-stream | "150 simultaneous channels" | ~30:1 |

- Entropy coding: "range asymmetric numeral systems (rANS)" with "a static codebook which is
  compiled into firmware", generated off-line from spike data.

**What this means for the interface:** the chip sees all 256 channels in succession every
frame (section 2.2), but the broadband modes (lossless, lossy) compress a **selected subset**
(2-4 or 4-8 channels), because the radio cannot carry more. For those modes most slots carry
channels that are not being compressed. The spike modes cover every channel.

**Latency tension:** the patent frames data in 10-40 ms windows; the challenge asks for
< 1 ms. Either the challenge's figure refers to per-sample processing (not packet delivery),
or windows differ per mode. (not specified)

## 4. Tiny Tapeout as the constraint envelope (TTSKY26d, sky130)

TT is used only to prove the design works under hard area/IO/power limits; it does not set
the architecture.

### 4.1 Area and storage
| | value | source |
|---|---|---|
| tile | "about 160x100 um", "about 1000 digital logic gates" (template: ~167x108 um) | [6], template |
| sizes | 1x1, 1x2, 2x2, 3x2, 4x2, 6x2, 8x2 (max 16 tiles) | template info.yaml |
| flip-flops | "approximately 320 DFFs (40 bytes) fit per tile" (fewer with logic) | [8] |
| latch RAM | 512 bits in one tile at 88% utilisation (example) | [8] |
| DFF RAM macro | RAM32: 32x32 bits in ~3x2 tiles | [8] |
| SRAM macros | IHP shuttles only (not sky130) | [8] |
| placement density | template default 60% (`PL_TARGET_DENSITY_PCT`) | template config.json |

**Derived:** Neuralink keeps 226 b/channel of spike state in SRAM for 256 channels (58 kbit).
On TT sky130, with no SRAM macro and ~320 DFFs or ~512 latch bits per tile, a full 16-tile
8x2 design holds roughly 5 kbit of DFFs or ~8 kbit of latches with no room for logic. So a TT
build can hold per-channel state for **a handful of channels, not 256**. That matches the
patent's 4-8 broadband channels.

### 4.2 Pins and clock
- 26 pins: `clk`, `rst_n`, 8 inputs, 8 outputs, 8 bidirectional (with `uio_oe`). [7]
- Inputs up to 66 MHz; outputs 33 MHz toggle; ~20 ns pad round trip; clock insertion delay
  up to ~10 ns. [7], [9]
- Clock: from the demo board's RP2040 (PWM/PIO), 1 Hz to 66.5 MHz; designs "around 66 MHz"
  max, FAQ says "at least 50MHz"; template constrains `CLOCK_PERIOD` 20 ns. [9], [6]

### 4.3 Power
- TT publishes **no per-project power or current limit**. [6]
- So the power constraint comes from Neuralink: ~10 uW per channel for compression *and*
  radio (section 1); the 2019 front end alone is 5.2 uW/channel [4].
- Measured on the routed TT design, or estimated with the local flow (`scripts/check.py`).

### 4.4 Feeding 256 channels: pins vs on-chip generator
- One ASIC's stream is 4.94 MS/s of 10-bit samples (section 2.2). Pins could carry the rate
  (8 + 2 input pins, 66 MHz input limit), but the RP2040 host cannot sustain real recordings
  at that rate for long (derived: its 264 KB of RAM holds 27-43 ms of a 4.94 MS/s stream at
  2 or 1.25 bytes per sample).
- Approach agreed for the TT build (**not built yet**: no generator in `src/`, T-GEN-1/2 open):
  **generate the 256-slot stream on chip** (peripheral, not
  part of the compressor), so the compressor sees the real interface (256 slots, slot-rate
  timing, frame marker) at full speed:
  - counter / ramp: deterministic, trivially checkable against the golden model;
  - LFSR noise: worst case for compression (incompressible, many escapes, maximum output
    rate) - a stress test;
  - both are deterministic, so the Python model reproduces the exact expected output.
- Real recordings still go through the pins at a reduced rate for bit-exact checks (the
  existing strobe protocol, `src/project.v`).
- Output: compressed bytes on `uo_out` with `m_valid/m_last`. For 8 lossy channels at
  ~2 bit/sample: 8 x 19.3 kHz x 2 b ~ 310 kbit/s (derived), well within pin and RP2040 limits.

## 5. Constraints for the testing phase (summary)

1. Input interface = 256-slot multiplexed stream, 10-bit samples, one sample per slot,
   slot 0 marked; ~16-20 kHz per channel (19.3 kHz nominal), no backpressure. [1], [4]
2. Broadband modes compress a selected 2-8 channels; spike modes cover all channels. [2]
3. Per-channel state is time-multiplexed (one row per channel). [1]
4. Latency: < 1 ms [3]; spike decision ~1 us [1]; packet windows 10-40 ms [2].
5. Power: < 10 mW for 1024 channels incl. radio (~10 uW/channel). [3]
6. TT build: <= 16 tiles, ~5-8 kbit storage total, 8/8/8 IO, <= ~50-66 MHz; on-chip
   256-slot stimulus generator plus slow real-data path through pins.
7. Test data: the challenge recordings (1 h, non-human primate motor cortex, 743 files,
   ~19.5 kHz, 10-bit codes stored as 16-bit WAV). [3], `model/nlc/data.py`

## 6. Not specified anywhere we found

- Chip clock frequency and the number of cycles per slot.
- Number of ASICs in N1 and how the 1024 channels split across them.
- Packet format, channel IDs/timestamps, how broadband channels are selected or switched.
- Radio energy per bit (the repo's 10 nJ/bit is unsourced).
- Process node, chip area.
- Whether compression latency (< 1 ms) is per sample or per delivered packet.

## Sources

1. US 2021/0012909 A1, *Real-time neural spike detection* (Neuralink).
   https://patents.google.com/patent/US20210012909A1/en
2. US 12,369,863 B2, *Neural signal compression for brain-machine interface* (Neuralink).
   https://patents.google.com/patent/US12369863B2/en
3. Neuralink Compression Challenge README. Live page now returns 404; quotes verified against
   the Internet Archive snapshot of 2024-12-25:
   https://web.archive.org/web/20241225214357/https://content.neuralink.com/compression-challenge/README.html
   ("N1 implant generates ~200Mbps of eletrode data (1024 electrodes @ 20kHz, 10b resolution)
   and can transmit ~1Mbps wirelessly. So > 200x compression is needed. Compression must run
   in real time (< 1ms) at low power (< 10mW, including radio).")
4. E. Musk & Neuralink, "An integrated brain-machine interface platform with thousands of
   channels", JMIR 2019. https://pmc.ncbi.nlm.nih.gov/articles/PMC6914248/
5. Neuralink, N1 implant description. https://www.neuralink.com/
6. Tiny Tapeout FAQ. https://www.tinytapeout.com/faq/
7. Tiny Tapeout GPIO specs. https://tinytapeout.com/specs/gpio/
8. Tiny Tapeout memory specs. https://tinytapeout.com/specs/memory/
9. Tiny Tapeout clock specs. https://tinytapeout.com/specs/clock/
