import numpy as np

from nlc.packet import decode_stream, encode_stream, stream_bits
from nlc.spikes import (BinnedConfig, BinnedSpikeCodec, FrontEndConfig, SbpConfig,
                        SpikeBandPowerCodec, SpikeFrontEnd, calibrate_thresholds, highpass)

FE = FrontEndConfig()


def test_highpass_matches_streaming_frontend(neural):
    fe = SpikeFrontEnd(FE, [1] * neural.shape[1])
    hp = np.array([fe.step(f)[0] for f in neural[:500]])
    assert np.array_equal(hp, highpass(neural[:500], FE))


def test_refractory_blocks_redetection():
    x = np.full((60, 1), 512)
    x[10:14, 0] = 400               # one wide negative excursion
    x[40, 0] = 400                  # second spike after refractory period
    fe = SpikeFrontEnd(FrontEndConfig(refractory=20), [50])
    spikes = [fe.step(f)[1][0] for f in x]
    assert [i for i, s in enumerate(spikes) if s] == [10, 40]


def test_binned_roundtrip_matches_counter(neural):
    thr = calibrate_thresholds(neural, FE)
    cfg = BinnedConfig(bin_len=400, group=4)
    pkts = encode_stream(neural, BinnedSpikeCodec(FE, cfg, thr))
    ref = BinnedSpikeCodec(FE, cfg, thr)
    expect = [ref.counts(neural[t:t + 400]) for t in range(0, len(pkts) * 400, 400)]
    got = decode_stream(pkts, BinnedSpikeCodec(FE, cfg, thr), neural.shape[1])
    assert np.array_equal(got, np.array(expect))
    assert got.sum() > 0


def test_binned_all_zero_group_costs_one_bit():
    x = np.full((400, 16), 512)
    pkts = encode_stream(x, BinnedSpikeCodec(FE, BinnedConfig(group=8), [50] * 16))
    assert pkts == [bytes([0x80, 0x00])]  # header(mode 2, seq 0) + '00' + pad


def test_binned_saturates():
    x = np.full((400, 1), 512)
    x[::25, 0] = 0  # 16 spikes, more than c_max
    codec = BinnedSpikeCodec(FrontEndConfig(refractory=5), BinnedConfig(group=1, c_max=7), [50])
    pkts = encode_stream(x, codec)
    assert decode_stream(pkts, codec, 1).tolist() == [[7]]


def test_sbp_roundtrip(neural):
    for metric in ("abs", "square"):
        cfg = SbpConfig(metric=metric, sbp_shift=6 if metric == "abs" else 12, sbp_floor=2)
        pkts = encode_stream(neural, SpikeBandPowerCodec(FE, cfg))
        ref = SpikeBandPowerCodec(FE, cfg)
        expect = [ref.values(neural[t:t + 400]) for t in range(0, len(pkts) * 400, 400)]
        got = decode_stream(pkts, SpikeBandPowerCodec(FE, cfg), neural.shape[1])
        assert np.array_equal(got, np.array(expect))
        assert stream_bits(pkts) < neural[: len(pkts) * 400].size  # < 1 bit/sample
