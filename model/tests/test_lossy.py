import numpy as np
import pytest

from nlc.bitstream import BitReader, BitWriter
from nlc.lossy import (LossyCodec, LossyConfig, channel_symbols, dwt_fwd, dwt_inv, fifo_profile,
                       layout, push_schedule, stream_coeffs, train_tables)
from nlc.packet import decode_stream, encode_stream, stream_bits
from nlc.rans import RansTables, normalize_freqs, rans_decode, rans_encode

HW_FIFO_DEPTH = 8          # src/nlc_lossy.sv QDEPTH


@pytest.mark.parametrize("levels", [1, 3, 5])
def test_integer_lifting_is_perfect_reconstruction(rng, levels):
    s = rng.integers(-512, 512, 64)
    assert np.array_equal(dwt_inv(dwt_fwd(s, levels)), s)


@pytest.mark.parametrize("levels,n", [(1, 16), (3, 64), (4, 128)])
def test_streaming_lifting_matches_block_transform(rng, levels, n):
    s = rng.integers(-512, 512, n)
    bands = dwt_fwd(s, levels)
    got = [np.full(len(b), -10**9) for b in bands]
    for pushed in stream_coeffs(s, levels):
        for k, p, c in pushed:
            got[0 if k == 0 else levels + 1 - k][p] = c
    for g, b in zip(got, bands):
        assert np.array_equal(g, b)


def test_production_order_covers_every_coefficient_once():
    cfg = LossyConfig()
    lay = layout(cfg)
    assert len(lay) == cfg.block_len == len(set(lay))
    assert sum(push_schedule(cfg)) == cfg.block_len


def test_fifo_depth_fits_hardware():
    peak, lag = fifo_profile(LossyConfig())
    assert peak <= HW_FIFO_DEPTH
    assert (peak, lag) == (7, 6)


def test_rans_roundtrip(rng):
    tables = RansTables([normalize_freqs(rng.integers(0, 1000, 10)) for _ in range(3)])
    ctxs = rng.integers(0, 3, 2000).tolist()
    syms = rng.integers(0, 10, 2000).tolist()
    data = rans_encode(list(zip(ctxs, syms)), tables)
    assert rans_decode(data, tables, ctxs) == syms


def test_normalize_freqs():
    f = normalize_freqs([0, 0, 5, 10_000])
    assert sum(f) == 4096 and min(f) >= 1


def test_lossless_when_unquantised(neural):
    cfg = LossyConfig(approx_shift=0, detail_shifts=(0, 0, 0))
    codec = LossyCodec(cfg, train_tables(neural, cfg))
    pkts = encode_stream(neural, codec)
    assert np.array_equal(decode_stream(pkts, codec, neural.shape[1]), neural)


def test_escapes_roundtrip(rng):
    """Full-scale noise: many escapes in every context, still exact when unquantised."""
    cfg = LossyConfig(approx_shift=0, detail_shifts=(0, 0, 0), s_max=3)
    x = rng.integers(0, 1024, (256, 4))
    codec = LossyCodec(cfg, train_tables(x, cfg))
    syms = [v for c in range(4) for _, v in channel_symbols(x[:64, c], cfg)]
    assert sum(abs(v) > cfg.s_max for v in syms) > 50
    assert np.array_equal(decode_stream(encode_stream(x, codec), codec, 4), x)


def test_payload_flushes_n_flush_states(neural):
    cfg = LossyConfig(n_flush=16)
    codec = LossyCodec(cfg, train_tables(neural, cfg))
    block = neural[:256]
    bw = BitWriter()
    codec.encode_payload(bw, block)
    y = codec.decode_payload(BitReader(bw.getvalue()), 256, block.shape[1])
    assert y.shape == block.shape


def test_lossy_quality_and_ratio(neural):
    cfg = LossyConfig()
    train, test = neural[:2048], neural[2048:]
    codec = LossyCodec(cfg, train_tables(train, cfg))
    pkts = encode_stream(test, codec)
    y = decode_stream(pkts, codec, test.shape[1])
    err = (y - test).astype(float)
    sig = test - test.mean(axis=0)
    snr_db = 10 * np.log10((sig ** 2).sum() / (err ** 2).sum())
    assert snr_db > 10
    assert stream_bits(pkts) / test.size < 5
