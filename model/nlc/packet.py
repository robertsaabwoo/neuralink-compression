"""Packet framing shared by all modes (the RTL bit packer's contract).

  header (8 bits) = mode[1:0] | seq[5:0]      seq increments per packet, wraps at 64
  payload         = mode-specific, see each codec
  pad             = zeros to the next byte boundary

Each packet covers `codec.frames_per_packet` frames of all channels. Only whole
packets are encoded; trailing frames are dropped. Payload length is implied by
the shared configuration (channel count, codec config), not transmitted.
"""

from __future__ import annotations

from enum import IntEnum

import numpy as np

from .bitstream import BitReader, BitWriter


class Mode(IntEnum):
    LOSSLESS = 0
    LOSSY = 1
    BINNED = 2
    SBP = 3


def encode_header(mode: int, seq: int) -> int:
    return ((mode & 0x3) << 6) | (seq & 0x3F)


def decode_header(byte: int) -> tuple[int, int]:
    return byte >> 6, byte & 0x3F


def encode_stream(x: np.ndarray, codec) -> list[bytes]:
    """x: (T, C) integer ADC codes, frame-major (one row = one sample per channel)."""
    codec.reset()
    fpp = codec.frames_per_packet
    packets = []
    for seq, t0 in enumerate(range(0, x.shape[0] - fpp + 1, fpp)):
        bw = BitWriter()
        bw.write(encode_header(codec.mode, seq), 8)
        codec.encode_payload(bw, x[t0:t0 + fpp])
        bw.align()
        packets.append(bw.getvalue())
    return packets


def decode_stream(packets: list[bytes], codec, n_channels: int) -> np.ndarray:
    """Concatenated decoder output: samples for modes 0/1, one row per packet for 2/3."""
    out = []
    for seq, pkt in enumerate(packets):
        br = BitReader(pkt)
        mode, pseq = decode_header(br.read(8))
        if mode != codec.mode or pseq != seq & 0x3F:
            raise ValueError(f"packet {seq}: header mode={mode} seq={pseq}")
        out.append(codec.decode_payload(br, codec.frames_per_packet, n_channels))
        br.align()
        if br.bits_left:
            raise ValueError(f"packet {seq}: {br.bits_left} trailing bits")
    return np.concatenate(out) if out else np.empty((0, n_channels), dtype=np.int64)


def decode_packet(pkt: bytes, codec, n_channels: int) -> tuple[int, np.ndarray]:
    """One packet on its own (packets are independent): (seq, samples)."""
    br = BitReader(pkt)
    mode, seq = decode_header(br.read(8))
    if mode != codec.mode:
        raise ValueError(f"header mode={mode}, expected {codec.mode}")
    y = codec.decode_payload(br, codec.frames_per_packet, n_channels)
    br.align()
    if br.bits_left:
        raise ValueError(f"{br.bits_left} trailing bits")
    return seq, y


def stream_bits(packets: list[bytes]) -> int:
    return 8 * sum(len(p) for p in packets)
