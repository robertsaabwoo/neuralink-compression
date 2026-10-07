import pytest

from nlc.bitstream import BitReader, BitWriter


def test_msb_first_packing():
    bw = BitWriter()
    bw.write(0b1, 1)
    bw.write(0b0110, 4)
    bw.align()
    assert bw.getvalue() == bytes([0b10110000])


def test_unary_and_truncated_unary():
    bw = BitWriter()
    bw.write_unary(3)                 # 1110
    bw.write_truncated_unary(2, 2)    # 11
    bw.write_truncated_unary(0, 2)    # 0
    bw.align()
    assert bw.getvalue() == bytes([0b11101100])
    br = BitReader(bw.getvalue())
    assert br.read_unary() == 3
    assert br.read_unary(limit=2) == 2
    assert br.read_unary(limit=2) == 0


def test_random_roundtrip(rng):
    fields = [(int(rng.integers(0, 1 << n)), n) for n in rng.integers(0, 33, 500)]
    bw = BitWriter()
    for v, n in fields:
        bw.write(v, n)
    bw.align()
    br = BitReader(bw.getvalue())
    assert [br.read(n) for _, n in fields] == [v for v, _ in fields]


def test_rejects_overflow_and_overread():
    with pytest.raises(ValueError):
        BitWriter().write(4, 2)
    with pytest.raises(EOFError):
        BitReader(b"\x00").read(9)
