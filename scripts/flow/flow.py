"""nlc check flow: model + RTL + gate-level tests, lint, synthesis, timing, power, compression,
scored against scripts/flow/budgets.json.

Runs inside the nlc-flow Docker image; launch it from the host with scripts/check.py.

  python scripts/flow/flow.py                     all steps
  python scripts/flow/flow.py --only synth,sta    some steps (later steps reuse reports/latest)
  python scripts/flow/flow.py --quick             compression on 32 held-out files (default 160)
  python scripts/flow/flow.py --update-baseline   store this run as the known-good baseline

Steps: model lint rtl core synth sta gl power compress algo
  core   test/core: nlc_core at the real 256-slot interface (T-IF, T-BW, T-OVF, T-ROB, T-LAT);
         --quick runs T-IF-1/3 only. Tests in KNOWN_FAIL fail on today's RTL on purpose (they
         state a requirement for the design phase): reported, not counted as failures.
  area   lossy core area vs N_SEL (T-AREA-2): fixed cost, per-channel slope, flops per channel
  gl     lossy netlist at 200 ns and at the TT clock (T-GL-1); TT top netlist (T-GL-2)
  power  gate-level scenario sims (docs/testing.md 4.4: op, op4, worst, floor; --quick: op)
  algo   scripts/algo_eval.py (T-ALG-2/3/5/7): rate, SNR, generalisation, spikes, max error
Output: reports/<UTC stamp>/ (logs, netlists, timing/power reports, summary.md, metrics.json),
mirrored to reports/latest/. Exit status 1 if any budget FAILs.

Numbers are pre-layout estimates (Yosys + OpenSTA, ideal clock, no wire parasitics). TT's GDS
action (LibreLane) is the sign-off for area and timing.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vcd_activity import activity_tcl  # noqa: E402

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PDK = Path(os.environ.get("NLC_PDK", "/pdk"))
LIB = {"tt": PDK / "lib/sky130_fd_sc_hd__tt_025C_1v80.lib",
       "ss": PDK / "lib/sky130_fd_sc_hd__ss_100C_1v60.lib",
       "ff": PDK / "lib/sky130_fd_sc_hd__ff_n40C_1v95.lib"}
LOSSY_SRC = ["nlc_lossy.sv", "nlc_lift53.sv", "nlc_rans.sv", "nlc_lossy_rom.v", "nlc_icg.sv"]
STEPS = ["model", "lint", "rtl", "core", "synth", "area", "sta", "gl", "power", "compress", "algo"]
AREA_SWEEP = [2, 4, 8, 16]    # N_SEL values for T-AREA-2 (SEL_W >= 1, so no N_SEL = 1)
# TT top-level tests for modes not in silicon (D1): skipped by the tests themselves
PENDING_TESTS = {"test_lossless", "test_binned", "test_sbp", "test_lossless_host_never_waits"}
# test/core tests that fail on today's RTL; name prefix -> reason (docs/testing.md section 3).
# A prefix may cover cases that pass (e.g. a stall short enough to be absorbed).
_ABORT = ("F2: disable mid-packet must end the packet with the abort token (D7); "
          "today the next run's packet 0 is glued to it")
_OVF = ("F3: a blocked output must abort the packet, flush and resume at the next packet (D6); "
        "today every later packet is corrupt")
_FRAME = ("F4: a short frame must repeat the missing channels' previous samples (D5); "
          "today the channel order slips for good")
KNOWN_FAIL = {
    "t_ovf_1": _OVF, "t_ovf_2": _OVF, "t_rob_4": _FRAME,     # t_rob_4 long/missing pass
    "t_if_3_reenable": _ABORT, "t_rob_2": _ABORT, "t_rob_3": _ABORT,
}
# functional coverage bins the T2 regression must hit (docs/testing.md 4.5)
COVERAGE_BINS = ([f"esc_ctx{c}" for c in range(4)] + ["esc_consecutive", "esc_last_symbol"]
                 + [f"occ_{k}" for k in range(8)]
                 + ["rans_hazard_stall", "back_to_back_same_channel"]
                 + [f"coder_word_bytes_{k}" for k in range(5)]
                 + ["flush_overlaps_samples", "out_fifo_full", "host_stall_header",
                    "host_stall_payload", "host_stall_flush"]
                 + [f"n_sel_{k}" for k in range(1, 9)]
                 + ["slot0_selected", "slot255_selected", "adjacent_selected", "seq_wrap"]
                 + [f"enable_drop_{p}" for p in ("midframe", "midblock", "midpacket", "flush")])
POWER_SCENARIOS = {   # name: (test in test/lossy, VCD start in frames: skip start-up)
    "op": ("test_power_op", 64), "op4": ("test_power_n4", 64),
    "worst": ("test_power_worst", 64), "floor": ("test_power_floor", 0),
    "idle": ("test_power_idle", 0)}

BUDGETS = json.loads((HERE / "budgets.json").read_text())
OP = BUDGETS["operating_point"]
# TT's GDS flow constrains CLOCK_PERIOD from src/config.json (template default 20 ns)
_cfg = ROOT / "src" / "config.json"
if _cfg.exists():
    OP["tt_clock_period_ns"] = float(json.loads(_cfg.read_text()).get("CLOCK_PERIOD", 20))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class Run:
    def __init__(self, out: Path) -> None:
        self.out = out
        self.metrics: dict[str, float] = {}
        self.info: dict[str, object] = {}
        self.notes: list[str] = []
        self.failed_tests: list[str] = []
        self.pending_tests: list[str] = []

    def sh(self, cmd: str, log: str, cwd: Path = ROOT, env: dict | None = None,
           timeout: int = 3600) -> tuple[int, str]:
        t0 = time.time()
        p = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True,
                           env={**os.environ, **(env or {})}, timeout=timeout)
        text = p.stdout + p.stderr
        (self.out / log).write_text(f"$ {cmd}\n(cwd {cwd}, {time.time() - t0:.0f} s, "
                                    f"exit {p.returncode})\n\n{text}")
        return p.returncode, text


def info_yaml() -> tuple[str, list[str], int]:
    text = (ROOT / "info.yaml").read_text()
    top = re.search(r'top_module:\s*"([^"]+)"', text).group(1)
    tiles = re.search(r'tiles:\s*"(\d+)x(\d+)"', text)
    srcs = re.findall(r'^\s*-\s*"([^"]+\.s?v)"', text, re.M)
    return top, srcs, int(tiles.group(1)) * int(tiles.group(2))


def junit(path: Path) -> list[tuple[str, bool]]:
    if not path.exists():
        return []
    out = []
    for tc in ET.parse(path).getroot().iter("testcase"):
        bad = any(c.tag in ("failure", "error") for c in tc)
        out.append((tc.get("name"), not bad))
    return out


def num(pattern: str, text: str, group: int = 1, default=None):
    m = re.search(pattern, text, re.M)
    return float(m.group(group)) if m else default


# ---------------------------------------------------------------------------
# steps
# ---------------------------------------------------------------------------

def step_model(r: Run) -> None:
    code, text = r.sh("python -m pytest -q -p no:cacheprovider", "model_pytest.log")
    passed = int(num(r"(\d+) passed", text, default=0))
    failed = int(num(r"(\d+) failed", text, default=0)) + int(num(r"(\d+) error", text, default=0))
    r.info["model_tests"] = f"{passed} passed, {failed} failed"
    if code or failed:
        r.failed_tests.append(f"model pytest ({failed} failed)")


def step_lint(r: Run) -> None:
    top, srcs, _ = info_yaml()
    strict = "-Wall -Wno-DECLFILENAME -Wno-UNUSEDSIGNAL -Wno-UNUSEDPARAM"
    _, t1 = r.sh(f"verilator --lint-only {strict} --top-module nlc_lossy {' '.join(LOSSY_SRC)}",
                 "lint_lossy.log", cwd=ROOT / "src")
    _, t2 = r.sh(f"verilator --lint-only -Wall -Wno-fatal --top-module {top} {' '.join(srcs)}",
                 "lint_top.log", cwd=ROOT / "src")
    lossy_issues = len(re.findall(r"^%(Error|Warning)-", t1, re.M))
    top_errors = len(re.findall(r"^%Error(?!: Exiting)", t2, re.M))
    r.metrics["lint_errors"] = lossy_issues + top_errors
    r.info["lint_warnings_top"] = len(re.findall(r"^%Warning-", t2, re.M))


def _cocotb(r: Run, cwd: Path, log: str, make_args: str = "") -> list[tuple[str, bool]]:
    shutil.rmtree(cwd / "sim_build", ignore_errors=True)
    (cwd / "results.xml").unlink(missing_ok=True)
    r.sh(f"make {make_args}", log, cwd=cwd)
    res = junit(cwd / "results.xml")
    if not res:
        r.failed_tests.append(f"{log}: no results (build error?)")
    return res


def step_rtl(r: Run) -> None:
    summary = {}
    for name, cwd, args in [("lossy", ROOT / "test/lossy", ""),
                            ("rans_static", ROOT / "test/rans", "TOP=static"),
                            ("rans_adaptive", ROOT / "test/rans", "TOP=adaptive")]:
        res = _cocotb(r, cwd, f"rtl_{name}.log", args)
        summary[name] = f"{sum(ok for _, ok in res)}/{len(res)}"
        r.failed_tests += [f"rtl {name}: {n}" for n, ok in res if not ok]
    r.sh("python scripts/gen_vectors.py --out test/vectors", "rtl_top_vectors.log")
    res = _cocotb(r, ROOT / "test", "rtl_top.log")
    for n, ok in res:
        if not ok and n in PENDING_TESTS:
            r.pending_tests.append(n)
        elif not ok:
            r.failed_tests.append(f"rtl top: {n}")
    summary["tt_top"] = f"{sum(ok for _, ok in res)}/{len(res)}"
    r.info["rtl_tests_passed"] = summary


def step_core(r: Run, quick: bool) -> None:
    res_dir = r.out / "core"
    filt = "COCOTB_TEST_FILTER=t_if_[13]" if quick else ""
    res = _cocotb(r, ROOT / "test/core", "rtl_core.log", f"NLC_RESULTS={res_dir} {filt}")
    known = []
    for n, ok in res:
        why = next((w for k, w in KNOWN_FAIL.items() if n.startswith(k)), None)
        if why and not ok:
            known.append(f"{n}: {why}")
        elif not ok:
            r.failed_tests.append(f"rtl core: {n}")
    fixed = [k for k in KNOWN_FAIL
             if any(n.startswith(k) for n, _ in res) and all(ok for n, ok in res if n.startswith(k))]
    r.info["rtl_core_passed"] = f"{sum(ok for _, ok in res)}/{len(res)}"
    r.info["known_failures"] = known
    r.notes += [f"{n} now passes: remove it from KNOWN_FAIL in scripts/flow/flow.py" for n in fixed]
    collect_core(r, res_dir)


def collect_core(r: Run, res_dir: Path) -> None:
    """Measured values from the test/core JSONs (written by test/env/nlc_env.py)."""
    runs = {p.stem: json.loads(p.read_text()) for p in sorted(res_dir.glob("*.json"))}
    if not runs:
        return
    lat = runs.get("t_if_1", {}).get("latency")      # real data, operating point (T-LAT-1)
    if lat:
        r.metrics["latency_processing_us"] = lat["processing_us"]["max"]
        r.metrics["latency_delivery_ms"] = lat["delivery_ms"]["max"]
        r.info["latency"] = lat
    clean = [v for v in runs.values() if "bandwidth" in v and not v["n_errors"]]
    if clean:
        worst = max(clean, key=lambda v: v["bandwidth"]["bytes_per_frame_max"])
        r.info["bandwidth_worst"] = {
            "test": worst["test"],
            **{k: v for k, v in worst["bandwidth"].items() if k != "packet_bytes"}}
        r.info["out_fifo_peak"] = max(v["bandwidth"]["out_fifo_peak"] for v in clean)
        r.info["flush_bytes_max"] = max(v["bandwidth"]["flush_bytes_max"] for v in clean)
    bw2 = runs.get("t_bw_2")
    if bw2:
        r.info["host_turnaround_max_clocks"] = bw2.get("host_turnaround_max_clocks")
        r.info["host_turnaround_trials"] = bw2.get("trials")
    cov: dict[str, int] = {}
    for v in runs.values():
        for k, n in v.get("coverage", {}).items():
            if isinstance(n, int):
                cov[k] = cov.get(k, 0) + n
    missing = [b for b in COVERAGE_BINS if not cov.get(b)]
    r.metrics["coverage_missing_bins"] = len(missing)
    r.info["coverage_missing"] = missing
    r.info["coverage"] = cov
    r.info["core_errors"] = {k: v["errors"][:3] for k, v in runs.items() if v["n_errors"]}


SYNTH = """read_liberty -lib {lib}
read_verilog -sv -I{src} {files}
hierarchy -check -top {top}
synth -top {top} {flatten}
dfflibmap -liberty {lib}
abc -liberty {lib}
setundef -zero
opt_clean -purge
{extra}
tee -o {stat} stat -liberty {lib}
"""
# constants -> tie cells, wire aliases -> buffers (OpenSTA's reader has no assign statements),
# then single-bit nets with plain names so VCD nets map to netlist nets one to one
NETLIST_EXTRA = """splitnets
hilomap -singleton -hicell sky130_fd_sc_hd__conb_1 HI -locell sky130_fd_sc_hd__conb_1 LO
insbuf -buf sky130_fd_sc_hd__buf_1 A X
opt_clean -purge
rename -hide w:* x:* %d
rename -hide c:*
rename -enumerate
write_verilog -noattr -noexpr -nohex -nodec {netlist}"""


def _area(stat: str) -> tuple[float, int, int]:
    top = stat.split("=== design hierarchy ===")[-1]
    area = num(r"Chip area for (?:top )?module.*?:\s*([\d.]+)", top, default=0.0)
    cells = int(num(r"Number of cells:\s*(\d+)", top, default=0))
    flops = sum(int(n) for n in re.findall(r"sky130_fd_sc_hd__e?df\w+\s+(\d+)", top))
    return area, cells, flops


def step_synth(r: Run) -> None:
    top, srcs, n_tiles = info_yaml()
    src = ROOT / "src"
    lib = LIB["tt"]
    jobs = {
        "lossy": ("nlc_lossy", LOSSY_SRC, "-flatten", True),
        "lossy_hier": ("nlc_lossy", LOSSY_SRC, "", False),
        "top": (top, srcs, "-flatten", True),
    }
    for name, (mod, files, flat, netlist) in jobs.items():
        extra = NETLIST_EXTRA.format(netlist=r.out / f"{name}_netlist.v") if netlist else ""
        script = SYNTH.format(src=src, files=" ".join(str(src / f) for f in files), top=mod,
                              flatten=flat, lib=lib, extra=extra, stat=r.out / f"{name}_stat.txt")
        (r.out / f"synth_{name}.ys").write_text(script)
        code, _ = r.sh(f"yosys -q -s {r.out / f'synth_{name}.ys'}", f"synth_{name}.log")
        if code:
            r.failed_tests.append(f"synthesis {name} (see synth_{name}.log)")
    stat = (r.out / "lossy_stat.txt").read_text()
    area, cells, flops = _area(stat)
    r.metrics["lossy_cell_area_um2"] = area
    r.metrics["lossy_area_per_ch_mm2"] = area / OP["n_channels"] / 1e6
    r.info["lossy_cells"] = cells
    r.info["lossy_flops"] = flops
    cell_area = {"sky130_fd_sc_hd__edfxtp_1": 30.0288, "sky130_fd_sc_hd__dfxtp_1": 20.0192}
    flop_area = sum(int(n) * cell_area[c] for c, n in
                    re.findall(r"(sky130_fd_sc_hd__e?dfxtp_1)\s+(\d+)", stat.split("===")[-1]))
    r.info["lossy_flop_area_share"] = round(flop_area / area, 3) if area else None
    hier = (r.out / "lossy_hier_stat.txt").read_text()
    r.info["lossy_area_by_module_um2"] = {
        re.sub(r"^\$paramod\$\w+\\", "", m.group(1)): float(m.group(2))
        for m in re.finditer(r"Chip area for module '\\?([^']+)':\s*([\d.]+)", hier)}
    tarea, tcells, tflops = _area((r.out / "top_stat.txt").read_text())
    r.info["top_cell_area_um2"] = tarea
    r.info["top_flops"] = tflops
    r.info["tiles"] = n_tiles
    r.metrics["top_utilisation"] = tarea / (n_tiles * OP["tt_tile_um2"])


def step_area(r: Run) -> None:
    """T-AREA-2: synthesise nlc_lossy for several N_SEL; least-squares line area = a + b * N."""
    src, lib = ROOT / "src", LIB["tt"]
    files = " ".join(str(src / f) for f in LOSSY_SRC)
    pts = {}
    for n in AREA_SWEEP:
        w = max(1, (n - 1).bit_length())
        stat = r.out / f"area_nsel{n}_stat.txt"
        script = SYNTH.format(src=src, files=files, top="nlc_lossy", flatten="-flatten", lib=lib,
                              extra="", stat=stat)
        script = script.replace("hierarchy -check -top nlc_lossy",
                                f"chparam -set N_SEL {n} -set SEL_W {w} nlc_lossy\n"
                                "hierarchy -check -top nlc_lossy\n"
                                # chparam makes the top a $paramod; synth -top needs the name
                                "rename -top nlc_lossy")
        (r.out / f"area_nsel{n}.ys").write_text(script)
        code, _ = r.sh(f"yosys -q -s {r.out / f'area_nsel{n}.ys'}", f"area_nsel{n}.log")
        if code or not stat.exists():
            r.failed_tests.append(f"area sweep N_SEL={n} (see area_nsel{n}.log)")
            continue
        area, cells, flops = _area(stat.read_text())
        pts[n] = {"area_um2": round(area), "cells": cells, "flops": flops}
    r.info["area_vs_n_sel"] = pts
    if len(pts) >= 2:
        n = list(pts)
        a = [pts[k]["area_um2"] for k in n]
        f = [pts[k]["flops"] for k in n]
        mn, ma, mf = sum(n) / len(n), sum(a) / len(a), sum(f) / len(f)
        var = sum((k - mn) ** 2 for k in n)
        slope = sum((k - mn) * (v - ma) for k, v in zip(n, a)) / var
        fslope = sum((k - mn) * (v - mf) for k, v in zip(n, f)) / var
        r.info["area_fixed_um2"] = round(ma - slope * mn)
        r.info["area_per_channel_um2"] = round(slope)
        r.info["flops_per_channel"] = round(fslope, 1)
        r.notes.append(f"area sweep: {r.info['area_fixed_um2']} um^2 fixed + "
                       f"{r.info['area_per_channel_um2']} um^2 and {fslope:.0f} flops per channel "
                       f"(Neuralink: 226 state bits/ch, docs/constraints.md C-AREA-4)")


STA = """read_liberty {lib}
read_verilog {netlist}
link_design {top}
create_clock -name clk -period {period} [get_ports clk]
set_input_delay  {io} -clock clk [delete_from_list [all_inputs] [get_ports clk]]
set_output_delay {io} -clock clk [all_outputs]
set_load 0.02 [all_outputs]
report_checks -path_delay {kind} -format full -digits 3 -group_count 5
exit
"""


def _sta(r: Run, netlist: Path, top: str, corner: str, period: float, kind: str, tag: str):
    tcl = STA.format(lib=LIB[corner], netlist=netlist, top=top, period=period,
                     io=round(0.2 * period, 3), kind=kind)
    (r.out / f"sta_{tag}.tcl").write_text(tcl)
    _, text = r.sh(f"sta -no_splash -exit {r.out / f'sta_{tag}.tcl'}", f"sta_{tag}.log")
    slacks = [float(s) for s in re.findall(r"^\s*(-?[\d.]+)\s+slack \((?:MET|VIOLATED)\)", text, re.M)]
    return min(slacks) if slacks else None


def step_sta(r: Run) -> None:
    top, _, _ = info_yaml()
    tt_clk, op_clk = OP["tt_clock_period_ns"], OP["clock_period_ns"]
    res = {}
    for name, mod in [("top", top), ("lossy", "nlc_lossy")]:
        net = r.out / f"{name}_netlist.v"
        if not net.exists():
            r.notes.append(f"sta: {net.name} missing (run synth)")
            return
        res[name] = {
            "setup_ss_tt_clk": _sta(r, net, mod, "ss", tt_clk, "max", f"{name}_setup_ss_{tt_clk:g}ns"),
            "setup_ss_op_clk": _sta(r, net, mod, "ss", op_clk, "max", f"{name}_setup_ss_{op_clk:g}ns"),
            "setup_tt_tt_clk": _sta(r, net, mod, "tt", tt_clk, "max", f"{name}_setup_tt_{tt_clk:g}ns"),
            "hold_ff": _sta(r, net, mod, "ff", tt_clk, "min", f"{name}_hold_ff"),
        }
    t = res["top"]
    r.metrics["setup_slack_ss_tt_clk_ns"] = t["setup_ss_tt_clk"]
    r.metrics["setup_slack_ss_op_clk_ns"] = t["setup_ss_op_clk"]
    r.metrics["hold_slack_ff_ns"] = t["hold_ff"]
    if t["setup_ss_tt_clk"] is not None:
        # worst path incl. setup and the 20% IO delay on IO paths; fmax ~ 1 / this
        r.info["critical_path_ss_ns"] = round(tt_clk - t["setup_ss_tt_clk"], 3)
    r.info["sta"] = res


def step_gl(r: Run) -> None:
    net = r.out / "lossy_netlist.v"
    if not net.exists():
        r.notes.append("gl: lossy_netlist.v missing (run synth)")
        return
    for tag, clk, filt, plus in [("op", OP["clock_period_ns"], "test_power_window", ""),
                                 ("tt_clock", OP["tt_clock_period_ns"], "test_power_window", "")]:
        res = _cocotb(r, ROOT / "test/lossy", f"gl_{tag}.log",
                      f"GATES=yes NETLIST={net} CLK_NS={clk} COCOTB_TEST_FILTER={filt} "
                      f"PLUSARGS={plus}" if plus else
                      f"GATES=yes NETLIST={net} CLK_NS={clk} COCOTB_TEST_FILTER={filt}")
        ok = bool(res) and all(o for _, o in res)
        if tag == "op" and not ok:
            r.failed_tests.append("gate-level lossy (bytes differ from the model)")
        if tag == "tt_clock":
            r.info["gl_unit_delay_ok_at_tt_clock"] = ok
            if not ok:
                r.notes.append(
                    f"gate-level sim with TT's unit delays (#1 per cell) fails at "
                    f"{OP['tt_clock_period_ns']} ns: TT's gl_test action would fail unless the "
                    "testbench clock is slower than the deepest logic path (in cells x 1 ns)")
    top_net = r.out / "top_netlist.v"            # T-GL-2: the TT top through the pins
    if top_net.exists():
        shutil.rmtree(ROOT / "test/sim_build/gl_local", ignore_errors=True)
        res = _cocotb(r, ROOT / "test", "gl_top.log",
                      f"GATES=local NETLIST={top_net} CLK_NS={OP['clock_period_ns']} "
                      f"COCOTB_TEST_FILTER=test_lossy")
        ok = bool(res) and all(o for _, o in res)
        r.info["gl_top_ok"] = ok
        if not ok:
            r.failed_tests.append("gate-level TT top (T-GL-2, see gl_top.log)")


POWER = """read_liberty {lib}
read_verilog {netlist}
link_design nlc_lossy
create_clock -name clk -period {period} [get_ports clk]
set_power_activity -input -activity 0
{activity}
report_power -digits 4
exit
"""


def _power(text: str) -> dict[str, dict[str, float]]:
    out = {}
    for group in ("Sequential", "Combinational", "Clock", "Macro", "Pad", "Total"):
        m = re.search(rf"^{group}\s+([-\d.eE+]+)\s+([-\d.eE+]+)\s+([-\d.eE+]+)\s+([-\d.eE+]+)",
                      text, re.M)
        if m:
            out[group] = {k: float(v) * 1e6 for k, v in
                          zip(("internal", "switching", "leakage", "total"), m.groups())}
    return out


def _scenario_vcd(r: Run, net: Path, name: str) -> Path | None:
    """Gate-level run of one power scenario (docs/testing.md 4.4) at the operating clock."""
    test, start_frames = POWER_SCENARIOS[name]
    vcd = r.out / f"power_{name}.vcd"
    period = OP["clock_period_ns"]
    start_ns = int(start_frames * 256 * period)
    res = _cocotb(r, ROOT / "test/lossy", f"gl_power_{name}.log",
                  f"GATES=yes NETLIST={net} CLK_NS={period} COCOTB_TEST_FILTER={test} "
                  f"PLUSARGS='+vcd={vcd} +vcd_start={start_ns}'")
    if not (res and all(o for _, o in res)):
        r.failed_tests.append(f"gate-level power scenario {name} (see gl_power_{name}.log)")
    return vcd if vcd.exists() else None


def step_power(r: Run, quick: bool = False) -> None:
    net = r.out / "lossy_netlist.v"
    if not net.exists():
        r.notes.append("power: lossy_netlist.v missing (run synth)")
        return
    period = OP["clock_period_ns"]
    results = {}
    # idle (enable = 0) is simulated too: with clock gating, what toggles depends
    # on the gate enables, which OpenSTA cannot infer without activity
    runs = []
    for name in (["op", "idle"] if quick else list(POWER_SCENARIOS)):
        vcd = _scenario_vcd(r, net, name)
        if vcd is None:
            continue
        # activities are per operating cycle (the VCD comes from a run at the operating clock)
        act_f, act_r = r.out / f"activity_{name}.tcl", r.out / f"activity_{name}_raw.tcl"
        r.info[f"activity_annotation_{name}"] = activity_tcl(net, vcd, LIB["tt"], act_f, act_r)
        runs.append((name, f"source {act_f}"))             # budgeted: glitch-free
        if name == "op":
            runs.append(("op_with_glitches", f"source {act_r}"))
        else:
            vcd.unlink()                                    # ~100 MB each; keep op only
    for tag, activity in runs:
        tcl = POWER.format(lib=LIB["tt"], netlist=net, period=period, activity=activity)
        (r.out / f"power_{tag}.tcl").write_text(tcl)
        _, text = r.sh(f"sta -no_splash -exit {r.out / f'power_{tag}.tcl'}", f"power_{tag}.log")
        results[tag] = _power(text)
        lost = len(re.findall(r"^Warning \d+: .* not found", text, re.M))
        if lost:
            r.failed_tests.append(f"power {tag}: {lost} activity annotations not applied "
                                  f"(see power_{tag}.log)")
    if "Total" in results.get("idle", {}):
        r.metrics["power_idle_uw"] = results["idle"]["Total"]["total"]
    if "Total" in results.get("worst", {}):
        r.metrics["power_worst_uw"] = results["worst"]["Total"]["total"]
    if "Total" in results.get("op4", {}) and "Total" in results.get("op", {}):
        r.info["power_per_channel_slope_uw"] = round(
            (results["op"]["Total"]["total"] - results["op4"]["Total"]["total"]) / 4, 3)
    if "Total" not in results.get("op", {}):
        r.notes.append("power: no op result (see gl_power_op.log, power_op.log)")
        return
    if "Total" not in results["op"]:
        r.notes.append("power: could not parse report_power (see power_op.log)")
        return
    tot = results["op"]["Total"]
    p_uw = tot["total"]
    r.metrics["lossy_power_uw"] = p_uw
    r.info["power_uw"] = {k: {g: round(v["total"], 2) for g, v in d.items() if v["total"]}
                          for k, d in results.items()}
    r.info["power_breakdown_uw"] = {k: round(v, 2) for k, v in tot.items()}
    r.info["flop_clock_pin_share"] = (round(results["idle"]["Total"]["total"] / p_uw, 3)
                                      if p_uw and "Total" in results["idle"] else None)
    area = r.metrics.get("lossy_cell_area_um2")
    if area:
        r.metrics["lossy_power_density_mw_cm2"] = (p_uw * 1e-3) / (area / OP["tt_density"] * 1e-8)
    samples = OP["n_channels"] * OP["sample_rate_hz"]
    e_sample_nj = p_uw * 1e-6 / samples * 1e9
    r.info["energy_per_sample_nj"] = round(e_sample_nj, 4)
    bps = r.metrics.get("bits_per_sample", 2.087)
    r.metrics["radio_energy_ratio"] = e_sample_nj / ((10 - bps) * OP["radio_nj_per_bit"])
    r.notes.append("power: pre-CTS (no clock-tree buffers), no wire capacitance, glitch-free "
                   "activity; the routed design will be higher (see op_with_glitches)")


def step_compress(r: Run, quick: bool) -> None:
    code = r"""
import json, sys, numpy as np
sys.path.insert(0, "model")
from pathlib import Path
from nlc.data import load_challenge, synthetic
from nlc.lossy import LossyCodec, LossyConfig, default_tables
from nlc.packet import encode_stream, decode_stream
groups = int(sys.argv[1]); codec = LossyCodec(LossyConfig(), default_tables())
bits = n = 0; snr = []; src = "challenge files 32.." + str(31 + 8 * groups)
for g in range(groups):
    if Path("data/raw").exists():
        _, x = load_challenge("data/raw", 8, first_file=32 + 8 * g)
    else:
        x = synthetic(8, 5 * 19531, seed=100 + g); src = "synthetic (data/raw missing)"
    pk = encode_stream(x, codec); y = decode_stream(pk, codec, 8); ref = x[:len(y)]
    bits += 8 * sum(map(len, pk)); n += ref.size
    for c in range(8):
        e = (y[:, c] - ref[:, c]).astype(float); s = (ref[:, c] - ref[:, c].mean()).astype(float)
        snr.append(10 * np.log10((s ** 2).sum() / max((e ** 2).sum(), 1e-9)))
print(json.dumps({"bits_per_sample": bits / n, "snr_median_db": float(np.median(snr)),
                  "snr_p10_db": float(np.percentile(snr, 10)), "samples": n, "source": src}))
"""
    (r.out / "compress.py").write_text(code)
    groups = 4 if quick else 20
    rc, text = r.sh(f"python {r.out / 'compress.py'} {groups}", "compress.log")
    if rc:
        r.failed_tests.append("compression eval (see compress.log)")
        return
    res = json.loads(text.strip().splitlines()[-1])
    r.metrics["bits_per_sample"] = res["bits_per_sample"]
    r.metrics["snr_median_db"] = res["snr_median_db"]
    r.info["compression"] = res


def step_algo(r: Run, quick: bool) -> None:
    out = r.out / "algo_eval.json"
    groups = "--groups 4 --train-groups 1" if quick else "--full"
    rc, _ = r.sh(f"python scripts/algo_eval.py {groups} --out {out}", "algo_eval.log",
                 timeout=4 * 3600)
    if rc or not out.exists():
        r.failed_tests.append("algo_eval (see algo_eval.log)")
        return
    res = json.loads(out.read_text())
    r.info["algo"] = res
    for k in ("table_generalisation_pct", "recon_max_abs_err"):
        if res.get(k) is not None:
            r.metrics[f"algo_{k}"] = res[k]


# ---------------------------------------------------------------------------
# scoring and report
# ---------------------------------------------------------------------------

def score(name: str, value) -> tuple[str, dict]:
    b = BUDGETS["metrics"].get(name)
    if b is None or value is None:
        return ("INFO" if value is not None else "N/A"), b or {}
    better = (lambda v, ref: v <= ref) if b["cmp"] == "<=" else (lambda v, ref: v >= ref)
    if not better(value, b["limit"]):
        return "FAIL", b
    status = "PASS" if better(value, b["target"]) else "WARN"
    base = BUDGETS["baseline"].get(name)
    if status == "PASS" and isinstance(base, (int, float)) and base:
        worse = (value - base) / abs(base) if b["cmp"] == "<=" else (base - value) / abs(base)
        if worse > 0.10:
            status = "WARN"
    return status, b


def fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.4g}"
    return str(v)


def report(r: Run, steps: list[str]) -> int:
    r.metrics["tests_failed"] = len(r.failed_tests)
    rows, worst = [], "PASS"
    order = ["PASS", "INFO", "N/A", "WARN", "FAIL"]
    for name in BUDGETS["metrics"]:
        if name not in r.metrics:
            continue
        v = r.metrics[name]
        st, b = score(name, v)
        rows.append((st, name, fmt(v), b.get("unit", ""), b.get("cmp", "") + " " + fmt(b.get("limit")),
                     fmt(b.get("target")), fmt(BUDGETS["baseline"].get(name))))
        if order.index(st) > order.index(worst):
            worst = st
    w = [max(len(x[i]) for x in rows + [("status", "metric", "value", "unit", "limit", "target", "baseline")])
         for i in range(7)]
    head = ("status", "metric", "value", "unit", "limit", "target", "baseline")
    lines = ["  ".join(h.ljust(w[i]) for i, h in enumerate(head)),
             "  ".join("-" * w[i] for i in range(7))]
    lines += ["  ".join(c.ljust(w[i]) for i, c in enumerate(row)) for row in rows]
    table = "\n".join(lines)
    print("\n" + table + "\n")
    for t in r.failed_tests:
        print(f"FAILED  {t}")
    for t in r.pending_tests:
        print(f"PENDING {t} (mode not implemented in src/nlc_encoder.v yet)")
    for t in r.info.get("known_failures", []):
        print(f"KNOWN   {t}")
    for n in r.notes:
        print(f"NOTE    {n}")
    print(f"\noverall: {worst}   (report: {r.out.relative_to(ROOT)}/summary.md)")

    md = [f"# nlc check flow: {worst}", "",
          f"Run {r.out.name}, steps: {', '.join(steps)}. Budgets: scripts/flow/budgets.json, "
          "rationale: docs/budgets.md.", "",
          "| status | metric | value | unit | limit | target | baseline |",
          "|---|---|---|---|---|---|---|"]
    md += ["| " + " | ".join(row) + " |" for row in rows]
    md += ["", "## Failed tests", ""] + ([f"- {t}" for t in r.failed_tests] or ["none"])
    md += ["", "## Known failures (requirements for the design phase)", ""] + (
        [f"- {t}" for t in r.info.get("known_failures", [])] or ["none"])
    md += ["", "## Notes", ""] + ([f"- {n}" for n in r.notes] or ["none"])
    md += ["", "## Details", "", "```json", json.dumps(r.info, indent=2, default=str), "```", ""]
    (r.out / "summary.md").write_text("\n".join(md))
    (r.out / "metrics.json").write_text(json.dumps(
        {"metrics": r.metrics, "info": r.info, "failed_tests": r.failed_tests,
         "pending_tests": r.pending_tests, "notes": r.notes, "overall": worst,
         "steps": steps, "run": r.out.name}, indent=2, default=str))
    return 1 if worst == "FAIL" else 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--only", default="", help="comma-separated steps")
    ap.add_argument("--skip", default="", help="comma-separated steps")
    ap.add_argument("--quick", action="store_true",
                    help="compression on 32 held-out files, not 160; core T-IF only; power op only")
    ap.add_argument("--update-baseline", action="store_true")
    args = ap.parse_args()
    steps = [s for s in STEPS if (not args.only or s in args.only.split(","))
             and s not in args.skip.split(",")]

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = ROOT / "reports" / stamp
    out.mkdir(parents=True)
    latest = ROOT / "reports" / "latest"
    # partial runs reuse artefacts (netlists, VCD) from the previous run
    if args.only and latest.exists():
        for f in latest.glob("*_netlist.v"):
            shutil.copy(f, out)
        for f in latest.glob("*_stat.txt"):
            shutil.copy(f, out)
        prev = latest / "metrics.json"
        if prev.exists():
            old = json.loads(prev.read_text())
            r_prev = old.get("metrics", {})
        else:
            r_prev = {}
    else:
        r_prev = {}
    r = Run(out)
    r.metrics.update({k: v for k, v in r_prev.items() if k != "tests_failed"})

    fns = {"model": step_model, "lint": step_lint, "rtl": step_rtl,
           "core": lambda run: step_core(run, args.quick), "synth": step_synth,
           "area": step_area,
           "sta": step_sta, "gl": step_gl, "power": lambda run: step_power(run, args.quick),
           "compress": lambda run: step_compress(run, args.quick),
           "algo": lambda run: step_algo(run, args.quick)}
    # compression first: power uses bits/sample for the radio comparison
    for s in sorted(steps, key=lambda s: (s != "compress", STEPS.index(s))):
        t0 = time.time()
        print(f"[{s}] ...", flush=True)
        try:
            fns[s](r)
        except Exception as e:  # keep going: report what worked
            r.failed_tests.append(f"step {s} crashed: {e!r}")
        print(f"[{s}] {time.time() - t0:.0f} s", flush=True)

    rc = report(r, steps)
    if latest.exists():
        shutil.rmtree(latest)
    shutil.copytree(out, latest)
    if args.update_baseline:
        BUDGETS["baseline"] = {k: round(v, 6) for k, v in r.metrics.items()
                               if k in BUDGETS["metrics"] and isinstance(v, (int, float))}
        (HERE / "budgets.json").write_text(json.dumps(BUDGETS, indent=2) + "\n")
        print("baseline updated in scripts/flow/budgets.json")
    sys.exit(rc)


if __name__ == "__main__":
    main()
