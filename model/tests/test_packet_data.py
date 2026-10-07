import wave

import numpy as np
import pytest

from nlc.data import load_challenge, read_wav, wav_to_adc
from nlc.energy import break_even, max_compute_budget
from nlc.lossless import LosslessCodec, RiceConfig
from nlc.packet import Mode, decode_header, decode_stream, encode_header, encode_stream


def test_header_and_seq_wrap():
    assert encode_header(Mode.SBP, 65) == 0b11_000001
    assert decode_header(0b01_101010) == (1, 42)
    x = np.full((70, 1), 512)
    pkts = encode_stream(x, LosslessCodec(RiceConfig(frames_per_packet=1)))
    assert [decode_header(p[0])[1] for p in pkts[62:66]] == [62, 63, 0, 1]


def test_decoder_rejects_wrong_mode():
    pkts = encode_stream(np.full((4, 1), 512), LosslessCodec(RiceConfig(frames_per_packet=4)))
    bad = [bytes([encode_header(Mode.BINNED, 0)]) + pkts[0][1:]]
    with pytest.raises(ValueError):
        decode_stream(bad, LosslessCodec(RiceConfig(frames_per_packet=4)), 1)


def _wav_values(codes):
    # hypothesised challenge encoding: 10-bit code rescaled to the 16-bit range
    return np.round(codes * 65535 / 1023).astype(np.int64) - 32768


def test_wav_to_adc_inverts_noninteger_step(rng):
    codes = rng.integers(100, 900, 5000)
    x16 = _wav_values(codes)
    got, lut = wav_to_adc(x16)
    assert np.array_equal(np.array([lut[c] for c in got.tolist()]), x16)
    assert np.array_equal(np.diff(got), np.diff(codes))   # offset may differ, deltas exact


def test_load_challenge_stacks_files(tmp_path, rng):
    for i, n in enumerate((1000, 1200)):
        with wave.open(str(tmp_path / f"{i}.wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(19531)
            w.writeframes(_wav_values(rng.integers(300, 700, n)).astype("<i2").tobytes())
    assert read_wav(tmp_path / "0.wav")[0] == 19531
    fs, x = load_challenge(tmp_path, 2)
    assert fs == 19531 and x.shape == (1000, 2)


def test_break_even():
    assert max_compute_budget(10, 3.5) == pytest.approx(65e-9)
    assert break_even(10, 3.5, 1e-12).pays_off
    assert not break_even(10, 9.99, 1e-9).pays_off
