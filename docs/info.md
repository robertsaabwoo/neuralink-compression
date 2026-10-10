## How it works

The chip compresses neural recordings. An ADC multiplexer delivers one 10-bit sample per slot,
256 slots per frame. The chip picks up to 8 configured slots (channels) and compresses each one:

1. a 3-level LeGall 5/3 integer wavelet on 64-sample blocks (streaming, no block buffer);
2. quantisation of the coefficients;
3. static rANS entropy coding with tables in ROM (escapes for rare values).

Every 256 frames (13 ms at 5 MHz) it emits one packet: a header byte {mode = 1, seq}, the coded
bytes, then the 8 final coder states. Typical output is ~2.1 bits/sample at ~18 dB SNR on the
Neuralink compression-challenge recordings. Packets are independent and decode on their own.

The clock is the ADC slot rate: 5 MHz, one slot per clock.

## How to test

Pins (`src/project.v`):

| pin | dir | use |
|---|---|---|
| `ui_in[7:0]` | in | sample bits 7:0, or a config byte while `cfg_en` = 1 |
| `uio[1:0]` | in | sample bits 9:8 |
| `uio[2]` | in | `s_strobe`: rising edge = one slot (or one config byte); asynchronous, synchronised on chip |
| `uio[3]` | in | `s_frame`: high with the strobe of slot 0 |
| `uio[4]` | out | `overflow`: sticky, the coder fell a frame behind and dropped a packet (cleared by enable = 0 or reset) |
| `uio[5]` | in | `cfg_en`: strobes carry config bytes, address then data; asynchronous, synchronised on chip |
| `uio[6]` | out | `m_valid` |
| `uio[7]` | out | `m_last`: this byte ends a packet |
| `uo_out[7:0]` | out | output byte; config readback (below) |

1. With `cfg_en` = 1, write (address, data) pairs: `0x00` = 0x01 (lossy mode), `0x01` = number
   of channels (1-8), `0x10 + i` = slot of channel i (strictly ascending), then `0x00` = 0x81
   (enable).
2. Feed samples: one strobe per slot, at most one every 2 clocks (the test vectors use 32-slot
   frames = 64 clocks). The chip is designed for one slot per clock and 256-clock frames, where
   no data can make it drop a packet; at the slower pin rate worst-case data (e.g. noise on
   every channel) can abort a packet, which shows as the abort token and on `uio[4]`.
3. Read bytes: every clock with `m_valid` = 1 carries a byte on `uo_out` (`m_last` = 1: the
   packet's last byte). `m_last` = 1 with `m_valid` = 0 is the abort token: drop the partial
   packet. All outputs are registered.
4. Config readback (while `CTRL.enable` = 0): `cfg_en` = 1, strobe an address byte, wait 4
   clocks, read the register on `uo_out` (CTRL reads `{enable, 0000000}`), drop `cfg_en`
   without a data strobe (nothing is written).

Decode with the Python model in the repository (`model/nlc/packet.py`, `decode_packet`).

## External hardware

None. A microcontroller (e.g. the TT demo board's RP2040) plays the ADC and the host.
