"""Algorithm change guard (docs/testing.md 4.3, T-CHG-*, plus T-ALG-4/6 and T-INF-2).

Run after every edit to the lossy algorithm (model/nlc/lossy.py, rans_tdm.py, the tables) or
to the lossy RTL:  python nlc.py algo   (or: pytest -k chg)

These tests check properties, not frozen bytes, so a legal algorithm change passes and a
broken one fails. Everything is read from LossyConfig; nothing hard-codes today's values,
except the RTL side, which is parsed from src/ (T-CHG-5). The one frozen check is T-CHG-7
(bitstream fingerprint), which makes format changes deliberate: `python nlc.py accept`.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import asdict, replace
from pathlib import Path

import numpy as np
import pytest

from nlc.data import synthetic
from nlc.lossless import zigzag
from nlc.lossy import (TABLES_PATH, LossyCodec, LossyConfig, channel_symbols, default_tables,
                       dequantize, dwt_fwd, dwt_inv, fifo_profile, quantize, train_tables)
from nlc.packet import decode_packet, decode_stream, encode_stream
from nlc.rans_tdm import check_static_cdf
from nlc.stimgen import selected

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
GOLDEN = Path(__file__).with_name("golden") / "lossy_bitstream.json"
FPP_MIN_FRAME_CLOCKS = {"256-slot mux, 1 slot/clock": 256, "TT pin path (C-IF-9)": 64}


# ---------------------------------------------------------------------------
# RTL constants (parsed from src/, so the guard sees RTL edits too)
# ---------------------------------------------------------------------------

def _int(pattern: str, text: str, what: str) -> int:
    m = re.search(pattern, text)
    if not m:
        raise AssertionError(f"could not find {what} in the RTL (pattern {pattern!r})")
    return int(m.group(1))


def rtl_constants() -> dict:
    lossy = (SRC / "nlc_lossy.sv").read_text()
    rans = (SRC / "nlc_rans.sv").read_text()
    shifts = {name: _int(rf"assign\s+{name}\s*=\s*quant\(.*?,\s*(\d+)\)\s*;", lossy, name)
              for name in ("q1", "q2", "q3", "qa3")}
    levels = len(re.findall(r"^\s*nlc_lift53\s+#", lossy, re.M))
    n_bits = _int(r"logic\s+\[(\d+):0\]\s+n;", lossy, "block frame counter n") + 1
    return {
        "adc_bits": _int(r"input\s+logic\s+\[(\d+):0\]\s+smp_data", lossy, "smp_data width") + 1,
        "block_len": 1 << n_bits,
        "levels": levels,
        "approx_shift": shifts["qa3"],
        "detail_shifts": tuple(shifts[f"q{l}"] for l in range(1, levels + 1)),
        "s_max": _int(r"localparam\s+int\s+S_MAX\s*=\s*(\d+)", lossy, "S_MAX"),
        "blocks_per_packet": 1 << _int(r"parameter\s+int\s+BPP_W\s*=\s*(\d+)", lossy, "BPP_W"),
        "esc_bytes": _int(r"parameter\s+int\s+RAW_B\s*=\s*(\d+)", rans, "RAW_B"),
        "prob_bits": _int(r"parameter\s+int\s+PB\s*=\s*(\d+)", rans, "PB"),
        "lsh": _int(r"parameter\s+int\s+LSH\s*=\s*(\d+)", rans, "LSH"),
        "state_bytes": _int(r"parameter\s+int\s+SB\s*=\s*(\d+)", rans, "SB"),
        "n_sym": _int(r"parameter\s+int\s+N_SYM\s*=\s*(\d+)", rans, "N_SYM"),
        "qw": _int(r"localparam\s+int\s+QW\s*=\s*(\d+)", lossy, "QW"),
        "qdepth": _int(r"localparam\s+int\s+QDEPTH\s*=\s*(\d+)", lossy, "QDEPTH"),
        "n_sel": _int(r"parameter\s+int\s+N_SEL\s*=\s*(\d+)", lossy, "N_SEL"),
    }


# ---------------------------------------------------------------------------
# test data
# ---------------------------------------------------------------------------

def edge_patterns(n: int, ch: int = 8) -> dict[str, np.ndarray]:
    t = np.arange(n)[:, None]
    sq = np.where(t % 2 == 0, 0, 1023) * np.ones((1, ch), dtype=np.int64)
    step = np.where((t // 37) % 2 == 0, 0, 1023) * np.ones((1, ch), dtype=np.int64)
    return {
        "all0": np.zeros((n, ch), dtype=np.int64),
        "all1023": np.full((n, ch), 1023, dtype=np.int64),
        "nyquist_square": sq,
        "steps": step,
        "lfsr": selected("lfsr", list(range(0, 256, 256 // ch))[:ch], n),
        "ramp": selected("ramp", list(range(ch)), n),
    }


def reference_reconstruction(x: np.ndarray, cfg: LossyConfig) -> np.ndarray:
    """Independent path: block DWT -> quantise -> dequantise -> inverse, no entropy coding."""
    B, out = cfg.block_len, np.empty_like(x)
    for c in range(x.shape[1]):
        for t0 in range(0, x.shape[0] - B + 1, B):
            s = x[t0:t0 + B, c].astype(np.int64) - (1 << (cfg.adc_bits - 1))
            bands = dwt_fwd(s, cfg.levels)
            shifts = [cfg.approx_shift] + list(cfg.detail_shifts[::-1])
            rec = [dequantize(quantize(b, sh), sh) for b, sh in zip(bands, shifts)]
            y = dwt_inv(rec) + (1 << (cfg.adc_bits - 1))
            out[t0:t0 + B, c] = np.clip(y, 0, (1 << cfg.adc_bits) - 1)
    return out


VARIANTS = {
    "default": LossyConfig(),
    "lossless_shifts": LossyConfig(approx_shift=0, detail_shifts=(0, 0, 0)),
    "block32_l2": LossyConfig(block_len=32, levels=2, detail_shifts=(2, 1), blocks_per_packet=8),
    "block128_l4": LossyConfig(block_len=128, levels=4, detail_shifts=(3, 2, 2, 1),
                               blocks_per_packet=2),
    "s_max15": LossyConfig(s_max=15),
    "coarse": LossyConfig(approx_shift=2, detail_shifts=(4, 3, 3)),
}


@pytest.fixture(scope="module")
def train_x():
    return synthetic(8, 4096, seed=21)


# ---------------------------------------------------------------------------
# T-CHG-1: round trip for every data source and config variant
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", list(VARIANTS))
def test_chg1_roundtrip_matches_independent_reference(name, train_x):
    """Entropy coding + streaming order must be lossless w.r.t. quantisation."""
    cfg = VARIANTS[name]
    codec = LossyCodec(cfg, train_tables(train_x, cfg))
    n = 2 * cfg.frames_per_packet
    sources = {"synthetic": synthetic(8, n, seed=5), **edge_patterns(n)}
    for src, x in sources.items():
        pkts = encode_stream(x, codec)
        y = decode_stream(pkts, codec, x.shape[1])
        ref = reference_reconstruction(x[:len(y)], cfg)
        assert np.array_equal(y, ref), f"{name}/{src}: decode != quantised reference"
        if cfg.approx_shift == 0 and not any(cfg.detail_shifts):
            assert np.array_equal(y, x[:len(y)]), f"{name}/{src}: shifts 0 must be lossless"


@pytest.mark.parametrize("seed", range(5))
def test_chg1_random_inputs_default_tables(seed):
    """Seeded random fuzz with the shipped ROM tables (escapes everywhere)."""
    rng = np.random.default_rng(seed)
    cfg = LossyConfig()
    codec = LossyCodec(cfg, default_tables())
    n_ch = int(rng.integers(1, 9))
    x = rng.integers(0, 1024, (cfg.frames_per_packet, n_ch))
    y = decode_stream(encode_stream(x, codec), codec, n_ch)
    assert np.array_equal(y, reference_reconstruction(x, cfg))


# ---------------------------------------------------------------------------
# T-CHG-2: the hardware's assumptions still hold for the current config
# ---------------------------------------------------------------------------

def value_range(cfg: LossyConfig) -> int:
    """Largest |symbol value| (before the escape test) over adversarial inputs."""
    worst = 0
    n = cfg.block_len * 4
    xs = list(edge_patterns(n).values()) + [synthetic(8, n, seed=s) for s in range(3)]
    t = np.arange(n)
    for period in (2, 3, 4, 6, 8, 16):        # full-scale square waves of several periods
        xs.append(np.where((t // (period // 2 or 1)) % 2 == 0, 0, 1023)[:, None])
    for x in xs:
        for c in range(x.shape[1]):
            for t0 in range(0, n - cfg.block_len + 1, cfg.block_len):
                for _, v in channel_symbols(x[t0:t0 + cfg.block_len, c], cfg):
                    worst = max(worst, abs(v))
    return worst


def test_chg2_hardware_assumptions_hold():
    cfg, hw = LossyConfig(), rtl_constants()
    problems = []
    peak, lag = fifo_profile(cfg)
    if peak + 1 > hw["qdepth"]:
        problems.append(f"FIFO peak {peak} needs depth >= {peak + 1}; nlc_lossy.sv QDEPTH = "
                        f"{hw['qdepth']}")
    vmax = value_range(cfg)
    if vmax.bit_length() + 1 > hw["qw"]:
        problems.append(f"symbol values reach +-{vmax}: need {vmax.bit_length() + 1} bits, "
                        f"nlc_lossy.sv QW = {hw['qw']}")
    if zigzag(-vmax).bit_length() > 8 * cfg.esc_bytes:
        problems.append(f"escape value {vmax} does not fit esc_bytes = {cfg.esc_bytes}")
    q_w = cfg.lsh + 8
    flush = q_w + 3 + 3 * hw["n_sel"] * cfg.state_bytes / 2
    for where, clocks in FPP_MIN_FRAME_CLOCKS.items():
        if flush > clocks:
            problems.append(f"packet flush needs ~{flush:.0f} clocks per frame, {where} has "
                            f"{clocks}")
    if 8 * cfg.state_bytes < cfg.prob_bits + cfg.lsh + 8:
        problems.append("state_bytes too small for the rANS state")
    if cfg.frames_per_packet & (cfg.frames_per_packet - 1):
        problems.append(f"frames_per_packet {cfg.frames_per_packet} is not a power of two "
                        "(RTL counts it with a binary counter)")
    for k, cdf in enumerate(default_tables()):
        try:
            check_static_cdf(cdf, cfg.rans_cfg(1))
        except ValueError as e:
            problems.append(f"table {k}: {e}")
        if len(cdf) != cfg.n_sym + 1:
            problems.append(f"table {k} has {len(cdf) - 1} symbols, config has {cfg.n_sym}")
    assert not problems, "the RTL needs a matching change:\n  " + "\n  ".join(problems)


# ---------------------------------------------------------------------------
# T-CHG-4: streaming order == block transform for every variant
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", list(VARIANTS))
def test_chg4_streaming_equals_block(name, rng):
    cfg = VARIANTS[name]
    x = rng.integers(0, 1024, cfg.block_len)
    syms = channel_symbols(x, cfg)
    s = x.astype(np.int64) - (1 << (cfg.adc_bits - 1))
    bands = dwt_fwd(s, cfg.levels)
    want = {}
    for i, b in enumerate(bands):
        ctx = 0 if i == 0 else cfg.levels + 1 - i
        want[ctx] = list(quantize(b, cfg.shift_for_ctx(ctx)))
    got = {k: [] for k in range(cfg.n_ctx)}
    prev = 0
    for k, v in syms:
        if k == 0:
            v = prev = v + prev           # undo the delta coding of the approximation band
        got[k].append(v)
    for k in range(cfg.n_ctx):
        assert got[k] == [int(v) for v in want[k]], f"{name}: context {k} differs"


# ---------------------------------------------------------------------------
# T-CHG-5: model <-> RTL constants and ROM in sync
# ---------------------------------------------------------------------------

def test_chg5_rtl_constants_match_model():
    cfg, hw = LossyConfig(), rtl_constants()
    want = {k: getattr(cfg, k) for k in ("adc_bits", "block_len", "levels", "approx_shift",
                                          "s_max", "blocks_per_packet", "esc_bytes",
                                          "prob_bits", "lsh", "state_bytes", "n_sym")}
    want["detail_shifts"] = tuple(cfg.detail_shifts)
    bad = [f"{k}: model {v}, RTL {hw[k]}" for k, v in want.items() if hw[k] != v]
    assert not bad, "LossyConfig and the RTL disagree:\n  " + "\n  ".join(bad)


def test_chg5_rom_matches_tables():
    sys.path.insert(0, str(ROOT / "scripts"))
    from gen_lossy_rom import rom_verilog
    d = json.loads(TABLES_PATH.read_text())
    want = rom_verilog(d["cdfs"], LossyConfig(), d["source"])
    assert (SRC / "nlc_lossy_rom.v").read_text() == want, (
        "src/nlc_lossy_rom.v does not match model/nlc/lossy_tables.json: regenerate both with "
        "scripts/gen_lossy_rom.py")


# ---------------------------------------------------------------------------
# T-CHG-7: bitstream fingerprint (format changes must be accepted on purpose)
# ---------------------------------------------------------------------------

def golden_fingerprint() -> dict:
    cfg = LossyConfig(n_flush=8)               # the RTL always flushes N_SEL = 8 states
    tables = default_tables()
    codec = LossyCodec(cfg, tables)
    n = 2 * cfg.frames_per_packet
    data = {"synthetic": synthetic(8, n, seed=1234),
            "lfsr": selected("lfsr", [3, 40, 41, 90, 128, 200, 254, 255], n)}
    out = {"config": asdict(cfg),
           "tables_sha256": hashlib.sha256(json.dumps(tables).encode()).hexdigest()}
    for name, x in data.items():
        pkts = encode_stream(x, codec)
        out[name] = {"packet_bytes": [len(p) for p in pkts],
                     "sha256": hashlib.sha256(b"".join(pkts)).hexdigest()}
    return out


def test_chg7_bitstream_unchanged():
    if not GOLDEN.exists():
        pytest.fail(f"{GOLDEN.relative_to(ROOT)} missing: run `python nlc.py accept`")
    want = json.loads(GOLDEN.read_text())
    got = golden_fingerprint()
    got["config"] = json.loads(json.dumps(got["config"]))      # tuples -> lists, like the file
    diff = [k for k in got if got[k] != want.get(k)]
    assert not diff, (f"bitstream changed ({', '.join(diff)}). If intended: `python nlc.py "
                      "accept`, then record the change in docs/testing.md section 3")


# ---------------------------------------------------------------------------
# T-ALG-4: packets decode independently;  T-ALG-6: edge inputs
# ---------------------------------------------------------------------------

def test_alg4_packets_are_independent():
    cfg = LossyConfig(n_flush=8)
    codec = LossyCodec(cfg, default_tables())
    x = synthetic(8, 3 * cfg.frames_per_packet, seed=9)
    pkts = encode_stream(x, codec)
    full = decode_stream(pkts, codec, 8)
    fpp = cfg.frames_per_packet
    for k in reversed(range(len(pkts))):
        seq, y = decode_packet(pkts[k], codec, 8)
        assert seq == k and np.array_equal(y, full[k * fpp:(k + 1) * fpp])


@pytest.mark.parametrize("name", list(edge_patterns(8)))
def test_alg6_edge_inputs(name):
    cfg = LossyConfig(n_flush=8)
    codec = LossyCodec(cfg, default_tables())
    x = edge_patterns(cfg.frames_per_packet)[name]
    pkts = encode_stream(x, codec)
    y = decode_stream(pkts, codec, x.shape[1])
    assert np.array_equal(y, reference_reconstruction(x, cfg))
    # 8 ch x 256 frames; worst case is every symbol escaped: raw + up to 2 renorm bytes
    assert len(pkts[0]) <= 1 + 8 * cfg.frames_per_packet * (cfg.esc_bytes + 2) + 8 * 3


# ---------------------------------------------------------------------------
# T-INF-2: the source lists agree
# ---------------------------------------------------------------------------

def test_inf2_source_lists_agree():
    info = set(re.findall(r'^\s*-\s*"([^"]+\.s?v)"', (ROOT / "info.yaml").read_text(), re.M))
    mk = re.search(r"^PROJECT_SOURCES\s*=\s*(.+)$", (ROOT / "test/Makefile").read_text(), re.M)
    make = set(mk.group(1).split())
    flow = (ROOT / "scripts/flow/flow.py").read_text()
    lossy = set(re.findall(r'"(\w+\.s?v)"', re.search(r"LOSSY_SRC = \[(.*?)\]", flow).group(1)))
    assert info == make, f"info.yaml vs test/Makefile: {sorted(info ^ make)}"
    assert lossy <= info, f"flow LOSSY_SRC not in info.yaml: {sorted(lossy - info)}"
    for f in info:
        assert (SRC / f).exists(), f"info.yaml lists missing src/{f}"
