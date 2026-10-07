import numpy as np
import pytest

from nlc.lossless import LosslessCodec, RiceConfig, select_k, unzigzag, wrap, zigzag
from nlc.packet import decode_stream, encode_stream, stream_bits


def test_hand_computed_vector():
    """Locks the exact bitstream; the first RTL test should reproduce this.

    header mode=0 seq=0                         -> 00000000
    x=512, first sample of packet: raw 10 bits  -> 1000000000
    x=515: e=3, m=6, A=16 N=1 -> k=4            -> '0' + '0110'
    bits 100000000000110 + pad                  -> 0x80 0x0C
    """
    codec = LosslessCodec(RiceConfig(frames_per_packet=2))
    pkts = encode_stream(np.array([[512], [515]]), codec)
    assert pkts == [bytes([0x00, 0x80, 0x0C])]


def test_zigzag_wrap():
    assert [zigzag(e) for e in (0, -1, 1, -2, 2)] == [0, 1, 2, 3, 4]
    assert all(unzigzag(zigzag(e)) == e for e in range(-600, 600))
    assert wrap(1023 - 0, 10) == -1 and wrap(0 - 1023, 10) == 1


def test_select_k():
    assert select_k(16, 1, 9) == 4
    assert select_k(0, 1, 9) == 0
    assert select_k(10**6, 1, 9) == 9


@pytest.mark.parametrize("fpp", [1, 7, 128])
def test_roundtrip_neural(neural, fpp):
    codec = LosslessCodec(RiceConfig(frames_per_packet=fpp))
    pkts = encode_stream(neural, codec)
    n = len(pkts) * fpp
    assert np.array_equal(decode_stream(pkts, codec, neural.shape[1]), neural[:n])


def test_roundtrip_worst_case_exercises_escape(rng):
    x = rng.integers(0, 1024, (512, 4))
    x[::2] = 0
    x[1::2] = 1023  # full-scale toggling: huge residuals, escape path
    codec = LosslessCodec()
    pkts = encode_stream(x, codec)
    assert np.array_equal(decode_stream(pkts, codec, 4), x)
    cfg = codec.cfg
    assert stream_bits(pkts) <= len(pkts) * 8 + x.size * cfg.max_codeword_bits + 8 * len(pkts)


def test_constant_signal_converges_to_one_bit():
    x = np.full((1024, 2), 300)
    pkts = encode_stream(x, LosslessCodec(RiceConfig(frames_per_packet=1024)))
    assert stream_bits(pkts) / x.size < 1.1


def test_compresses_neural(neural):
    pkts = encode_stream(neural, LosslessCodec())
    assert stream_bits(pkts) / neural.size < 8  # < 10-bit raw
