import math

import pytest

from nlc.rans_tdm import TdmRansConfig, TdmRansDecoder, TdmRansEncoder

CFG = TdmRansConfig()


def laplace_symbols(rng, n, scale=3.0):
    """Skewed symbols around 32, like quantised wavelet coefficients."""
    v = rng.laplace(0, scale, n).round().astype(int) + 32
    return [int(s) for s in v.clip(0, 63)]


def round_robin(n_used, rounds):
    return [c for _ in range(rounds) for c in range(n_used)]


def roundtrip(packets):
    enc, dec = TdmRansEncoder(CFG), TdmRansDecoder(CFG)
    blobs = [enc.encode_packet(ch, sy) for ch, sy in packets]
    for (ch, sy), blob in zip(packets, blobs):
        assert dec.decode_packet(blob, ch) == sy
    return blobs


def test_roundtrip_multiple_packets_adapts(rng):
    packets = []
    for rounds in (8, 20, 20):
        ch = round_robin(CFG.n_ch, rounds)
        packets.append((ch, laplace_symbols(rng, len(ch))))
    blobs = roundtrip(packets)
    # same statistics: packets after the first code better once the table has adapted
    assert len(blobs[2]) < len(blobs[0]) * 20 / 8


def test_unused_channels_flush_initial_state(rng):
    ch = round_robin(24, 10)
    roundtrip([(ch, laplace_symbols(rng, len(ch)))])


def test_count_cap_keeps_m_bounded(rng):
    ch = round_robin(CFG.n_ch, 130)          # 4160 symbols > count cap
    sy = [int(s) for s in rng.integers(0, 64, len(ch))]
    enc = TdmRansEncoder(CFG)
    enc.encode_packet(ch, sy)
    assert enc.cdf[-1] == CFG.m_max
    roundtrip([(ch, sy), (ch, sy)])


def test_extreme_tables(rng):
    """One dominant symbol drives f_s close to M (the old renorm overflow case)."""
    ch = round_robin(CFG.n_ch, 100)
    sy = [7] * len(ch)
    sy[5] = 63
    roundtrip([(ch, sy), (ch, sy), (ch, [63] * len(ch))])


def test_rate_close_to_entropy(rng):
    ch = round_robin(CFG.n_ch, 60)
    sy = laplace_symbols(rng, len(ch))
    enc = TdmRansEncoder(CFG)
    enc.encode_packet(ch, sy)                 # train the table
    blob = enc.encode_packet(ch, sy)
    counts = [sy.count(s) for s in range(64)]
    h = -sum(k / len(sy) * math.log2(k / len(sy)) for k in counts if k)
    payload_bits = 8 * (len(blob) - 4 * CFG.n_ch)
    assert payload_bits / len(sy) < h * 1.05 + 0.1


def test_corrupt_packet_detected(rng):
    ch = round_robin(CFG.n_ch, 4)
    blob = TdmRansEncoder(CFG).encode_packet(ch, laplace_symbols(rng, len(ch)))
    with pytest.raises(ValueError):
        TdmRansDecoder(CFG).decode_packet(blob[1:], ch)


# ---------------------------------------------------------------------------
# static variant
# ---------------------------------------------------------------------------

from nlc.rans_tdm import (TdmRansStaticDecoder, TdmRansStaticEncoder,  # noqa: E402
                          build_static_cdf, uniform_static_cdf)


def trained_tables(rng, scales=(1.0, 2.0, 4.0, 8.0)):
    out = []
    for sc in scales:
        sy = laplace_symbols(rng, 20000, sc)
        out.append(build_static_cdf([sy.count(s) for s in range(64)], CFG))
    return out


def test_static_roundtrip_with_contexts(rng):
    tables = trained_tables(rng)
    enc, dec = TdmRansStaticEncoder(CFG, tables), TdmRansStaticDecoder(CFG, tables)
    for rounds in (5, 30):
        ch = round_robin(CFG.n_ch, rounds)
        ctx = [int(k) for k in rng.integers(0, CFG.n_ctx, len(ch))]
        sy = laplace_symbols(rng, len(ch))
        assert dec.decode_packet(enc.encode_packet(ch, sy, ctx), ch, ctx) == sy


def test_static_uniform_reset_table_is_valid(rng):
    enc = TdmRansStaticEncoder(CFG)
    assert enc.cdfs[0] == uniform_static_cdf(CFG) and enc.cdfs[0][-1] == 4096
    ch = round_robin(24, 6)
    sy = [int(s) for s in rng.integers(0, 64, len(ch))]
    blob = enc.encode_packet(ch, sy, [0] * len(ch))
    assert TdmRansStaticDecoder(CFG).decode_packet(blob, ch, [0] * len(ch)) == sy


def test_static_rejects_bad_tables():
    bad = list(range(65))                     # sums to 64, not 4096
    with pytest.raises(ValueError):
        TdmRansStaticEncoder(CFG, [bad] * CFG.n_ctx)
    flat = uniform_static_cdf(CFG)
    flat[10] = flat[9]                        # f_9 = 0
    with pytest.raises(ValueError):
        TdmRansStaticEncoder(CFG, [flat] * CFG.n_ctx)
