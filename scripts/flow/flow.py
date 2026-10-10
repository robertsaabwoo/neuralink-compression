"""nlc check flow: model + RTL + gate-level tests, lint, synthesis, timing, power, compression,
scored against scripts/flow/budgets.json.

Runs inside the nlc-flow Docker image; launch it from the host with scripts/check.py.

  python scripts/flow/flow.py                     all steps
  python scripts/flow/flow.py --only synth,sta    some steps (later steps reuse reports/latest)
  python scripts/flow/flow.py --quick             compression on 32 held-out files (default 160)
  python scripts/flow/flow.py --update-baseline   store this run as the known-good baseline

Steps: model lint rtl core synth area sta gl power layout compress algo
  core   test/core: nlc_core at the real 256-slot interface (T-IF, T-BW, T-OVF, T-ROB, T-LAT);
         --quick runs T-IF-1/3 only. Tests in KNOWN_FAIL fail on today's RTL on purpose (they
         state a requirement for the design phase): reported, not counted as failures.
  area   lossy core area vs N_SEL (T-AREA-2): fixed cost, per-channel slope, flops per channel
  gl     lossy netlist at 200 ns and at the TT clock (T-GL-1); TT top netlist (T-GL-2)
  power  gate-level scenario sims (docs/testing.md 4.4: op, op4, worst, floor; --quick: op)
  layout the routed design of TT's GDS action (python scripts/fetch_gds.py first): T-PWR-3
         real-data power with the clock tree, wires and repair buffers (vs the same scenario
         on our pre-layout top netlist); T-FAN-1 buffer trees and slews per RTL signal
         (layout_fanout.md); LibreLane's area by cell class
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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import netlist as NL  # noqa: E402
from vcd_activity import activity_tcl  # noqa: E402

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PDK = Path(os.environ.get("NLC_PDK", "/pdk"))
LIB = {"tt": PDK / "lib/sky130_fd_sc_hd__tt_025C_1v80.lib",
       "ss": PDK / "lib/sky130_fd_sc_hd__ss_100C_1v60.lib",
       "ff": PDK / "lib/sky130_fd_sc_hd__ff_n40C_1v95.lib"}
LOSSY_SRC = ["nlc_lossy.sv", "nlc_lift53.sv", "nlc_rans.sv", "nlc_lossy_rom.v", "nlc_icg.sv"]
STEPS = ["model", "lint", "rtl", "core", "synth", "area", "sta", "gl", "power", "layout",
         "compress", "algo"]
AREA_SWEEP = [2, 4, 8, 16]    # N_SEL values for T-AREA-2 (SEL_W >= 1, so no N_SEL = 1)
# TT top-level tests for modes not in silicon (D1): none since the RTL is lossy only
PENDING_TESTS: set[str] = set()
# test/core tests that fail on today's RTL; name prefix -> reason (docs/testing.md section 3).
# A prefix may cover cases that pass (e.g. a stall short enough to be absorbed).
_ABORT = ("F2: disable mid-packet must end the packet with the abort token (D7); "
          "today the next run's packet 0 is glued to it")
_OVF = ("F3: a blocked output must abort the packet, flush and resume at the next packet (D6); "
        "today every later packet is corrupt")
_FRAME = ("F4: a short frame must repeat the missing channels' previous samples (D5); "
          "today the channel order slips for good")
KNOWN_FAIL: dict[str, str] = {}     # D5-D7 implemented 2026-10-08 (was: t_ovf_1/2, t_rob_2/3/4)
# functional coverage bins the T2 regression must hit (docs/testing.md 4.5)
COVERAGE_BINS = ([f"esc_ctx{c}" for c in range(4)] + ["esc_consecutive", "esc_last_symbol"]
                 + [f"occ_{k}" for k in (1, 2, 4)]     # burst buffer after a push
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
# T-PWR-2: the whole core (config, slot selector, encoder, output FIFO) at the real interface
CORE_POWER_SCENARIOS = {"core_op": ("t_pwr_op", 64), "core_idle": ("t_pwr_idle", 0)}

BUDGETS = json.loads((HERE / "budgets.json").read_text())
# static timing / power engine: OpenSTA here; in CI (cts_preview workflow) OpenROAD, which runs
# the same Tcl and can also estimate wire parasitics from a placed database
STA = os.environ.get("NLC_STA", "sta -no_splash -exit")
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


# Parallel cocotb: compile once, then run jobs (one test case, or one runtime variant
# such as a clock period) as separate simulator processes from a work queue, longest
# first (durations of the last run in <suite>/.durations.json). Icarus is single
# threaded; NLC_JOBS processes (default CPUs - 2) share one container.
JOBS = int(os.environ.get("NLC_JOBS", max(1, (os.cpu_count() or 2) - 2)))


def list_tests(cwd: Path, modules: str) -> list[str]:
    """Full names of the cocotb tests in <cwd>/<module>.py (comma-separated modules)
    that are not skipped."""
    code = ("import importlib, sys; sys.path[:0] = [{cwd!r}, {env!r}]\n"
            "for m in {mods!r}:\n"
            "    for v in vars(importlib.import_module(m)).values():\n"
            "        if type(v).__name__ == 'TestGenerator':\n"
            "            for c in v.generate_tests():\n"
            "                if not c.skip: print(c.fullname)\n").format(
        cwd=str(cwd), env=str(ROOT / "test/env"), mods=modules.split(","))
    p = subprocess.run([sys.executable, "-c", code], cwd=cwd, capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": f"{cwd}:{ROOT / 'test/env'}"})
    return p.stdout.split()


def cocotb_jobs(r: Run, cwd: Path, tag: str, build: str,
                jobs: list[tuple[str, str, str]]) -> dict[str, list[tuple[str, bool]]]:
    """Build <cwd> once with make args `build`, then run jobs (name, filter regex, extra
    make args) in parallel. Logs: <tag>_build.log and <tag>/<name>.log in the report."""
    shutil.rmtree(cwd / "sim_build", ignore_errors=True)
    logs = r.out / tag
    logs.mkdir(exist_ok=True)
    tmp = cwd / "sim_build" / "results"
    code, _ = r.sh(f"make {build} COCOTB_TEST_FILTER=__build_only__ "
                   f"COCOTB_RESULTS_FILE={cwd / 'sim_build' / 'build.xml'}", f"{tag}_build.log", cwd=cwd)
    vvps = [p.relative_to(cwd) for p in (cwd / "sim_build").rglob("sim.vvp")]
    if not vvps:
        r.failed_tests.append(f"{tag}: build failed (see {tag}_build.log)")
        return {}
    # jobs never rebuild, even if a source changes during the run (make -o: "old file")
    build = build + "".join(f" -o {p.as_posix()}" for p in vvps)
    tmp.mkdir(parents=True, exist_ok=True)
    dur_file = cwd / ".durations.json"
    try:
        durations = json.loads(dur_file.read_text())
    except (OSError, ValueError):
        durations = {}
    order = sorted(jobs, key=lambda j: -durations.get(j[0], 1e9))   # unknown first, then longest

    def run(job):
        name, filt, extra = job
        safe = re.sub(r"[^\w.=-]+", "_", name)
        xml = tmp / f"{safe}.xml"
        t0 = time.time()
        r.sh(f"make {build} {extra} COCOTB_RESULTS_FILE={xml}", f"{tag}/{safe}.log", cwd=cwd,
             env={"COCOTB_TEST_FILTER": filt})
        return name, junit(xml), time.time() - t0

    out = {}
    with ThreadPoolExecutor(max_workers=JOBS) as pool:
        for name, res, dt in pool.map(run, order):
            out[name] = res
            durations[name] = round(dt, 1)
            if not res:
                r.failed_tests.append(f"{tag} {name}: no results (see {tag}/)")
    dur_file.write_text(json.dumps(durations, indent=1, sort_keys=True))
    return out


def cocotb_parallel(r: Run, cwd: Path, tag: str, module: str, build: str = "",
                    only: str = "") -> list[tuple[str, bool]]:
    """All (non-skipped) tests of a suite, one job per test case; `only` = regex filter."""
    names = [n for n in list_tests(cwd, module) if re.search(only, n)]
    res = cocotb_jobs(r, cwd, tag, build, [(n, f"^{re.escape(n)}$", "") for n in names])
    return [t for n in names for t in res.get(n, [])]


def step_rtl(r: Run) -> None:
    summary = {}
    res = cocotb_parallel(r, ROOT / "test/lossy", "rtl_lossy", "test_lossy")
    summary["lossy"] = f"{sum(ok for _, ok in res)}/{len(res)}"
    r.failed_tests += [f"rtl lossy: {n}" for n, ok in res if not ok]
    for name, args in [("rans_static", "TOP=static"), ("rans_adaptive", "TOP=adaptive")]:
        res = _cocotb(r, ROOT / "test/rans", f"rtl_{name}.log", args)   # seconds each
        summary[name] = f"{sum(ok for _, ok in res)}/{len(res)}"
        r.failed_tests += [f"rtl {name}: {n}" for n, ok in res if not ok]
    r.sh("python scripts/gen_vectors.py --out test/vectors", "rtl_top_vectors.log")
    res = cocotb_parallel(r, ROOT / "test", "rtl_top", "test_plumbing,test_modes")
    for n, ok in res:
        if not ok and n in PENDING_TESTS:
            r.pending_tests.append(n)
        elif not ok:
            r.failed_tests.append(f"rtl top: {n}")
    summary["tt_top"] = f"{sum(ok for _, ok in res)}/{len(res)}"
    r.info["rtl_tests_passed"] = summary


def step_core(r: Run, quick: bool) -> None:
    res_dir = r.out / "core"
    res = cocotb_parallel(r, ROOT / "test/core", "rtl_core", "test_core",
                          build=f"NLC_RESULTS={res_dir}", only="t_if_[13]" if quick else "")
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


def _latches(stat: str) -> int:
    top = stat.split("=== design hierarchy ===")[-1]
    return sum(int(n) for n in re.findall(r"sky130_fd_sc_hd__dlx\w+\s+(\d+)", top))


def step_synth(r: Run) -> None:
    top, srcs, n_tiles = info_yaml()
    src = ROOT / "src"
    lib = LIB["tt"]
    jobs = {
        "lossy": ("nlc_lossy", LOSSY_SRC, "-flatten", True),
        "lossy_hier": ("nlc_lossy", LOSSY_SRC, "", False),
        "top": (top, srcs, "-flatten", True),
        # the system without the TT pin glue (project.v): power scenario T-PWR-2
        "core": ("nlc_core", [f for f in srcs if f != "project.v"], "-flatten", True),
    }

    def synth(item):
        name, (mod, files, flat, netlist) = item
        extra = NETLIST_EXTRA.format(netlist=r.out / f"{name}_netlist.v") if netlist else ""
        script = SYNTH.format(src=src, files=" ".join(str(src / f) for f in files), top=mod,
                              flatten=flat, lib=lib, extra=extra, stat=r.out / f"{name}_stat.txt")
        (r.out / f"synth_{name}.ys").write_text(script)
        code, _ = r.sh(f"yosys -q -s {r.out / f'synth_{name}.ys'}", f"synth_{name}.log")
        return name, code

    with ThreadPoolExecutor(max_workers=JOBS) as pool:
        for name, code in pool.map(synth, jobs.items()):
            if code:
                r.failed_tests.append(f"synthesis {name} (see synth_{name}.log)")
    stat = (r.out / "lossy_stat.txt").read_text()
    area, cells, flops = _area(stat)
    r.metrics["lossy_cell_area_um2"] = area
    r.metrics["lossy_area_per_ch_mm2"] = area / OP["n_channels"] / 1e6
    r.info["lossy_cells"] = cells
    r.info["lossy_flops"] = flops
    r.info["lossy_latches"] = _latches(stat)
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
    r.info["top_latches"] = _latches((r.out / "top_stat.txt").read_text())
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
        pts[n] = {"area_um2": round(area), "cells": cells, "flops": flops,
                  "latches": _latches(stat.read_text())}
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


STA_TCL = """read_liberty {lib}
read_verilog {netlist}
link_design {top}
create_clock -name clk -period {period} [get_ports clk]
set_input_delay  {io} -clock clk [delete_from_list [all_inputs] [get_ports clk]]
set_output_delay {io} -clock clk [all_outputs]
set_load 0.02 [all_outputs]
report_checks -path_delay {kind} -format full -digits 3 -group_count 5
{extra}exit
"""
# latch rows (nlc_lreg): a path into an open latch borrows time and reports slack 0; the
# worst flop / output slack (paths launched by latches include the borrow) and the
# smallest unused borrow (max - actual) are reported instead
STA_MAX_EXTRA = """report_checks -path_delay max -format full -digits 3 -group_count 5 \
    -to [concat [all_registers -edge_triggered -data_pins] [all_outputs]]
"""


def _sta(r: Run, netlist: Path, top: str, corner: str, period: float, kind: str, tag: str):
    tcl = STA_TCL.format(lib=LIB[corner], netlist=netlist, top=top, period=period,
                         io=round(0.2 * period, 3), kind=kind,
                         extra=STA_MAX_EXTRA if kind == "max" else "")
    (r.out / f"sta_{tag}.tcl").write_text(tcl)
    _, text = r.sh(f"{STA} {r.out / f'sta_{tag}.tcl'}", f"sta_{tag}.log")
    slacks, spare = [], []
    for path in text.split("Startpoint:")[1:]:
        m = re.search(r"^\s*(-?[\d.]+)\s+slack \((?:MET|VIOLATED)\)", path, re.M)
        if not m:
            continue
        if "time borrowed from endpoint" in path:
            mx = num(r"max time borrow\s+(-?[\d.]+)", path, default=None)
            act = num(r"actual time borrow\s+(-?[\d.]+)", path, default=None)
            if mx is not None and act is not None:
                spare.append(mx - act)
            continue
        slacks.append(float(m.group(1)))
    if spare:
        r.info.setdefault("sta_latch_borrow_spare_ns", {})[tag] = round(min(spare), 3)
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
    top_net = r.out / "top_netlist.v"            # T-GL-2: the TT top through the pins

    def gl_top():
        if not top_net.exists():
            return None
        shutil.rmtree(ROOT / "test/sim_build/gl_local", ignore_errors=True)
        return _cocotb(r, ROOT / "test", "gl_top.log",
                       f"GATES=local NETLIST={top_net} CLK_NS={OP['clock_period_ns']} "
                       "COCOTB_TEST_FILTER='test_lossy$'")

    with ThreadPoolExecutor(max_workers=1) as pool:
        top_res = pool.submit(gl_top)
        lossy = cocotb_jobs(r, ROOT / "test/lossy", "gl", f"GATES=yes NETLIST={net}", [
            (tag, "test_power_window", f"CLK_NS={clk}")
            for tag, clk in [("op", OP["clock_period_ns"]), ("tt_clock", OP["tt_clock_period_ns"])]])
        top_res = top_res.result()
    for tag in ("op", "tt_clock"):
        res = lossy.get(tag, [])
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
    if top_res is not None:
        ok = bool(top_res) and all(o for _, o in top_res)
        r.info["gl_top_ok"] = ok
        if not ok:
            r.failed_tests.append("gate-level TT top (T-GL-2, see gl_top.log)")


POWER = """read_liberty {lib}
read_verilog {netlist}
link_design {top}
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


def _scenario_vcds(r: Run, cwd: Path, net: Path, tag: str,
                   scenarios: dict[str, tuple[str, int]]) -> dict[str, Path]:
    """Gate-level runs of power scenarios (docs/testing.md 4.4) at the operating clock, in
    parallel on one build of the netlist. Returns the VCD of each scenario that passed."""
    period = OP["clock_period_ns"]
    jobs = []
    for name, (test, start_frames) in scenarios.items():
        vcd = r.out / f"power_{name}.vcd"
        jobs.append((name, f"{test}$", f"CLK_NS={period} "
                     f"PLUSARGS='+vcd={vcd} +vcd_start={int(start_frames * 256 * period)}'"))
    res = cocotb_jobs(r, cwd, tag, f"GATES=yes NETLIST={net}", jobs)
    out = {}
    for name in scenarios:
        vcd = r.out / f"power_{name}.vcd"
        if not (res.get(name) and all(o for _, o in res[name])):
            r.failed_tests.append(f"gate-level power scenario {name} (see {tag}/)")
        elif vcd.exists():
            out[name] = vcd
    return out


def _report_power(r: Run, net: Path, top: str, tag: str, activity: str) -> dict:
    tcl = POWER.format(lib=LIB["tt"], netlist=net, top=top, period=OP["clock_period_ns"],
                       activity=activity)
    (r.out / f"power_{tag}.tcl").write_text(tcl)
    _, text = r.sh(f"{STA} {r.out / f'power_{tag}.tcl'}", f"power_{tag}.log")
    lost = len(re.findall(r"^Warning \d+: .* not found", text, re.M))
    if lost:
        r.failed_tests.append(f"power {tag}: {lost} activity annotations not applied "
                              f"(see power_{tag}.log)")
    return _power(text)


def step_power_core(r: Run, vcds: dict[str, Path]) -> None:
    """T-PWR-2: nlc_core at gate level (everything but the TT pin glue)."""
    net = r.out / "core_netlist.v"
    res = {}
    for name, vcd in vcds.items():
        act_f, act_r = r.out / f"activity_{name}.tcl", r.out / f"activity_{name}_raw.tcl"
        r.info[f"activity_annotation_{name}"] = activity_tcl(net, vcd, LIB["tt"], act_f, act_r)
        res[name] = _report_power(r, net, "nlc_core", name, f"source {act_f}")
        vcd.unlink()
    for name, metric in [("core_op", "core_power_uw"), ("core_idle", "core_power_idle_uw")]:
        if "Total" in res.get(name, {}):
            r.metrics[metric] = res[name]["Total"]["total"]
    r.info["core_power_uw"] = {k: {g: round(v["total"], 2) for g, v in d.items() if v["total"]}
                               for k, d in res.items()}


def step_power(r: Run, quick: bool = False) -> None:
    net = r.out / "lossy_netlist.v"
    if not net.exists():
        r.notes.append("power: lossy_netlist.v missing (run synth)")
        return
    results = {}
    # idle (enable = 0) is simulated too: with clock gating, what toggles depends
    # on the gate enables, which OpenSTA cannot infer without activity
    runs = []
    names = ["op", "idle"] if quick else list(POWER_SCENARIOS)
    core_net = r.out / "core_netlist.v"
    with ThreadPoolExecutor(max_workers=1) as pool:       # both suites' gate-level runs at once
        core = pool.submit(_scenario_vcds, r, ROOT / "test/core", core_net, "gl_power_core",
                           CORE_POWER_SCENARIOS) if core_net.exists() else None
        vcds = _scenario_vcds(r, ROOT / "test/lossy", net, "gl_power",
                              {n: POWER_SCENARIOS[n] for n in names})
        core_vcds = core.result() if core else {}
    step_power_core(r, core_vcds)
    for name, vcd in vcds.items():
        # activities are per operating cycle (the VCD comes from a run at the operating clock)
        act_f, act_r = r.out / f"activity_{name}.tcl", r.out / f"activity_{name}_raw.tcl"
        r.info[f"activity_annotation_{name}"] = activity_tcl(net, vcd, LIB["tt"], act_f, act_r)
        runs.append((name, f"source {act_f}"))             # budgeted: glitch-free
        if name == "op":
            runs.append(("op_with_glitches", f"source {act_r}"))
        else:
            vcd.unlink()                                    # ~100 MB each; keep op only
    for tag, activity in runs:
        results[tag] = _report_power(r, net, "nlc_lossy", tag, activity)
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


# ---------------------------------------------------------------------------
# layout: the routed design from TT's GDS action (scripts/fetch_gds.py -> data/gds/<sha7>/)
# ---------------------------------------------------------------------------

GDS = ROOT / "data" / "gds"
PHYS_LINE = re.compile(r"^\s*sky130_fd_sc_hd__(fill|decap|tapvpwrvgnd)_\d+\s+\S+\s*\(\);\s*$", re.M)
# T-PWR-3 scenarios (test/test_power.py), VCD start in frames of 256 clocks
LAYOUT_SCENARIOS = {"op": ("t_pwr3_op", 64), "idle": ("t_pwr3_idle", 0)}
POWER_VECTORS = ("python scripts/gen_vectors.py --out test/vectors --name power --source real "
                 "--n-slots 128 --frames 512 --slots 1 20 21 45 64 100 126 127")
SLEW_TCL = """{design}
set f [open {out} w]
foreach p [get_pins *] {{
  puts $f "[get_full_name $p] [get_property $p slew_max_rise] [get_property $p slew_max_fall]"
}}
close $f
report_checks -path_delay max -digits 3 -format end
report_checks -path_delay min -digits 3 -format end
puts "nlc_ws setup"
report_worst_slack -max -digits 4
puts "nlc_ws hold"
report_worst_slack -min -digits 4
exit
"""
LAYOUT_POWER_TCL = """{design}
set_power_activity -input -activity 0
source {activity}
report_power -digits 4
report_power -instances [get_cells *] -digits 6
exit
"""


def _gds_dir(r: Run) -> Path | None:
    latest = GDS / "LATEST"
    if not latest.exists():
        r.notes.append("layout: no routed design; run `python scripts/fetch_gds.py` on the host")
        return None
    d = GDS / latest.read_text().strip()
    src = json.loads((d / "source.json").read_text())
    r.info["layout_source"] = {k: src[k] for k in ("commit", "run_id", "url")}
    known = os.environ.get("NLC_GDS_MATCHES_SRC")      # from scripts/check.py (git on the host)
    if known in ("0", "1"):
        p = subprocess.CompletedProcess([], 1 - int(known), "", "")
    else:
        try:
            p = subprocess.run(["git", "-c", "safe.directory=*", "diff", "--quiet", src["commit"],
                                "--", "src", "info.yaml"], cwd=ROOT, capture_output=True, text=True)
        except FileNotFoundError:               # no git in the nlc-flow image
            p = subprocess.CompletedProcess([], 2, "", "git not installed")
    if p.returncode == 1:
        r.notes.append(f"layout: src/ or info.yaml changed since {src['commit'][:7]} (the routed "
                       "design): numbers are for that commit; push and fetch_gds.py for HEAD")
    elif p.returncode:
        r.notes.append(f"layout: could not compare src/ with {src['commit'][:7]} ({p.stderr.strip()})")
    r.info["layout_matches_src"] = p.returncode == 0
    return d


def _design_tcl(d: Path, lib: Path, net: Path, top: str) -> str:
    """Load the design with its parasitics: a routed run reads the extracted SPEF; a cts-preview
    (fetch_gds.py) estimates them from placement in OpenROAD (`NLC_STA` must be openroad), on
    the routing layers LibreLane uses for sky130 (RT_MIN/MAX_LAYER met1-met4)."""
    if (d / "nom.spef").exists():
        return (f"read_liberty {lib}\nread_verilog {net}\nlink_design {top}\n"
                f"read_sdc {d / 'signoff.sdc'}\nread_spef {d / 'nom.spef'}")
    return (f"read_db {d / 'design.odb'}\nread_liberty {lib}\nread_sdc {d / 'signoff.sdc'}\n"
            "set_wire_rc -signal -layers {met1 met2 met3 met4}\n"
            "set_wire_rc -clock -layers {met1 met2 met3 met4}\n"
            "estimate_parasitics -placement")


def _slews(r: Run, net: Path, top: str, d: Path, corner: str) -> dict[str, float]:
    """Worst (rise/fall) slew at every pin, layout parasitics, sign-off constraints. Also
    stores the corner's worst setup/hold slack in r.info["layout_sta_ws"]."""
    out = r.out / f"layout_slews_{corner}.txt"
    tcl = SLEW_TCL.format(design=_design_tcl(d, LIB[corner], net, top), out=out)
    (r.out / f"layout_sta_{corner}.tcl").write_text(tcl)
    _, text = r.sh(f"{STA} {r.out / f'layout_sta_{corner}.tcl'}", f"layout_sta_{corner}.log")
    r.info.setdefault("layout_sta_ws", {})[corner] = {
        # OpenSTA: "worst slack 1.23"; OpenROAD: "worst slack max 1.23"
        k: num(rf"^nlc_ws {k}\s*\nworst slack (?:max |min )?(-?[\d.]+)", text)
        for k in ("setup", "hold")}
    slews = {}
    if out.exists():
        for line in out.read_text().splitlines():
            t = line.split()
            if len(t) == 3:
                try:
                    slews[t[0]] = max(float(t[1]), float(t[2]))
                except ValueError:
                    pass
        out.unlink()
    return slews


def _instance_power(text: str) -> dict[str, float]:
    """report_power -instances: instance -> total uW."""
    out = {}
    for m in re.finditer(r"^\s*([-\d.eE+]+)\s+([-\d.eE+]+)\s+([-\d.eE+]+)\s+([-\d.eE+]+)\s+(\S+)\s*$",
                         text, re.M):
        out[m.group(5).lstrip("\\")] = float(m.group(4)) * 1e6
    return out


def _fanout_table(nl, trees, slews: dict[str, dict[str, float]], limit: float, lib, n: int = 25):
    """Buffer trees of the data nets, worst first (by leaves): what each RTL signal costs."""
    def pins_of(t):
        ps = [f"{i}/{p}" for i, p in t.leaves if i]
        for b in t.buffers:
            c = nl.cells[b]
            ps += [f"{b}/{p}" for p in list(c.ins) + list(c.outs)]
        return ps
    rows = []
    for t in sorted(trees.values(), key=lambda t: (-len(t.leaves), -len(t.buffers)))[:n]:
        ps = pins_of(t)
        row = {"signal": NL.named_source(nl, t.root), "driver": t.driver,
               "leaves": len(t.leaves), "buffers": len(t.buffers), "depth": t.depth,
               "buffer_area_um2": round(sum(lib.area.get(nl.cells[b].type, 0) for b in t.buffers)),
               "buffer_types": dict(sorted(NL._count(nl.cells[b].type for b in t.buffers).items(),
                                           key=lambda x: -x[1]))}
        for corner, s in slews.items():
            v = [s[p] for p in ps if p in s]
            row[f"max_slew_{corner}_ns"] = round(max(v), 3) if v else None
            row[f"slew_violations_{corner}"] = sum(x > limit for x in v)
        rows.append(row)
    return rows


def _md_table(rows: list[dict], cols: list[str]) -> list[str]:
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for row in rows:
        out.append("| " + " | ".join(
            ", ".join(f"{k} {v}" for k, v in row[c].items()) if isinstance(row[c], dict)
            else fmt(row[c]) for c in cols) + " |")
    return out


def step_layout(r: Run, quick: bool = False) -> None:
    """T-PWR-3 + T-FAN-1 on the routed design (docs/testing.md): real-data power with the clock
    tree CTS built, wire parasitics and repair buffers; buffer trees and slews per RTL signal."""
    d = _gds_dir(r)
    if d is None:
        return
    src = json.loads((d / "source.json").read_text())
    top, preview = src["top"], src.get("kind") == "cts-preview"
    r.info["layout_kind"] = src.get("kind", "routed")
    net = r.out / "layout_netlist.v"            # without fill/tap/decap (no logic, no ports)
    net.write_text(PHYS_LINE.sub("", (d / "nl.v").read_text()))
    lib = NL.read_lib(LIB["tt"])
    nl = NL.read_netlist(net, lib)

    # --- structure: LibreLane's own numbers, clock tree, buffer trees ---------------------
    ll = json.loads((d / "metrics.json").read_text())
    core = ll.get("design__core__area") or 1
    r.metrics["layout_utilisation"] = ll.get("design__instance__area__stdcell", 0) / core
    if preview:
        r.notes.append(f"layout: CTS PREVIEW ({src.get('last_step')}): placed, clock tree built, "
                       "not routed; wire parasitics estimated from placement, hold slack from "
                       "our STA after post-CTS repair (routing adds hold buffers later); slew counts are "
                       "not comparable with a routed run (estimated wires, routing-stage repair to "
                       "come)")
    r.info["layout_librelane"] = {
        "core_area_um2": core,
        "stdcell_area_um2": round(ll.get("design__instance__area__stdcell", 0)),
        "area_by_class_um2": {k.split(":")[-1]: round(v) for k, v in ll.items()
                              if k.startswith("design__instance__area__class:")},
        "setup_ws_ns": ll.get("timing__setup__ws"), "hold_ws_ns": ll.get("timing__hold__ws"),
        "max_slew_violations": ll.get("design__max_slew_violation__count"),
        "max_cap_violations": ll.get("design__max_cap_violation__count"),
        "max_fanout_violations": ll.get("design__max_fanout_violation__count"),
        "wirelength_um": ll.get("route__wirelength")}
    # a preview's hold metric is from before the post-CTS repair: replaced by our STA below
    r.metrics["layout_hold_slack_ns"] = None if preview else ll.get("timing__hold__ws")
    r.info["layout_area_by_class_um2"] = NL.class_area(nl, lib)
    r.info["layout_hold_buffers"] = sum(NL.cell_class(nl, c) == "hold_buffer"
                                        for c in nl.cells.values())
    roots = NL.clock_roots(nl, lib)
    clock = NL.clock_tree_summary(nl, lib, roots)
    r.info["layout_clock_tree"] = clock
    if (d / "cts.rpt").exists():                 # OpenROAD's own CTS summary
        cts = (d / "cts.rpt").read_text()
        used, _, dummies = cts.partition("Dummys used:")
        cells = r"sky130_fd_sc_hd__(\w+):\s*(\d+)"
        r.info["layout_cts_report"] = {
            "clock_roots": num(r"Clock Roots:\s*(\d+)", cts),
            "buffers_inserted": num(r"Buffers Inserted:\s*(\d+)", cts),
            "sinks": num(r"number of Sinks:\s*(\d+)", cts),
            "cells": {c: int(n) for c, n in re.findall(cells, used)},
            "dummies": {c: int(n) for c, n in re.findall(cells, dummies)}}
    trees = NL.trees(nl)
    buffered = [t for t in trees.values() if t.buffers]
    r.info["layout_buffer_trees"] = {
        "trees": len(buffered), "buffers": sum(len(t.buffers) for t in buffered),
        "buffer_area_um2": round(sum(lib.area.get(nl.cells[b].type, 0)
                                     for t in buffered for b in t.buffers))}

    # --- slews with routed parasitics (T-FAN-1) -------------------------------------------
    limit = num(r"set_max_transition\s+([\d.]+)", (d / "signoff.sdc").read_text(), default=0.75)
    slews = {c: _slews(r, net, top, d, c) for c in ("ss", "tt", "ff")}
    holds = [v["hold"] for v in r.info.get("layout_sta_ws", {}).values() if v["hold"] is not None]
    if r.metrics["layout_hold_slack_ns"] is None and holds:
        r.metrics["layout_hold_slack_ns"] = min(holds)
    clock_pins = {f"{c.inst}/{p}" for c in nl.cells.values()
                  for p, n in (c.ins | c.outs).items() if n in nl.clock_nets}
    for c, s in slews.items():
        ck = [v for p, v in s.items() if p in clock_pins]
        r.metrics[f"layout_slew_violations_{c}"] = sum(v > limit for v in s.values())
        r.info[f"layout_max_slew_{c}_ns"] = round(max(s.values()), 3) if s else None
        r.info[f"layout_clock_max_slew_{c}_ns"] = round(max(ck), 3) if ck else None
    rows = _fanout_table(nl, trees, slews, limit, lib)
    worst = sorted(trees.values(), key=lambda t: -max(
        (slews["ss"].get(f"{i}/{p}", 0) for i, p in t.leaves if i), default=0))[:10]
    slow = _fanout_table(nl, {t.root: t for t in worst}, slews, limit, lib, n=10)
    slow.sort(key=lambda x: -(x["max_slew_ss_ns"] or 0))
    r.info["layout_fanout_top"] = rows[:10]
    cols = ["signal", "driver", "leaves", "buffers", "depth", "buffer_area_um2", "buffer_types",
            "max_slew_ss_ns", "slew_violations_ss", "max_slew_tt_ns", "slew_violations_tt"]
    md = [f"# Buffer trees of the routed design ({d.name}, slew limit {limit} ns)", "",
          "Every data net with a real driver, expanded through the buffers the resizer put "
          "below it. `signal`: the net, or for an anonymous net the RTL-named nets upstream of "
          "its driver. `leaves`: real sinks. Slews: worst rise/fall over the tree's pins, routed "
          "parasitics (nom SPEF), sign-off constraints.", "", "## Highest fanout", ""]
    md += _md_table(rows, cols)
    md += ["", "## Slowest (worst slew at a leaf, ss)", ""] + _md_table(slow, cols)
    md += ["", "## Clock tree", "", "```json", json.dumps(clock, indent=2), "```", ""]

    # --- real-data power of the routed netlist vs our pre-layout netlist (T-PWR-3) ----------
    r.sh(POWER_VECTORS, "layout_vectors.log")
    vcfg = ROOT / "test/vectors/power/config.json"
    r.info["layout_power_data"] = json.loads(vcfg.read_text()).get("source") if vcfg.exists() else None
    if r.info["layout_power_data"] != "real":
        r.notes.append("layout: power scenario on SYNTHETIC data (data/raw missing): compare "
                       "only with runs on synthetic data")
    period = OP["clock_period_ns"]
    names = ["op"] if quick else list(LAYOUT_SCENARIOS)
    variants = [("layout", net)]
    pre = r.out / "top_netlist.v"
    if pre.exists():
        variants.append(("prelayout", pre))
    power: dict[str, dict] = {}
    inst_power: dict[str, dict[str, float]] = {}
    for tag, netlist in variants:
        jobs = []
        for name in names:
            test, start = LAYOUT_SCENARIOS[name]
            vcd = r.out / f"{tag}_{name}.vcd"
            jobs.append((name, f"{test}$", f"PLUSARGS='+vcd={vcd} "
                         f"+vcd_start={int(start * 256 * period)}'"))
        res = cocotb_jobs(r, ROOT / "test", f"gl_{tag}",
                          f"GATES=local NETLIST={netlist} FST= COCOTB_TEST_MODULES=test_power "
                          f"CLK_NS={period}", jobs)
        for name in names:
            vcd = r.out / f"{tag}_{name}.vcd"
            if not (res.get(name) and all(o for _, o in res[name])):
                r.failed_tests.append(f"{tag} netlist, power scenario {name}: bytes differ from "
                                      f"the model or no output (see gl_{tag}/)")
                continue
            if not vcd.exists():
                continue
            act_f, act_r = r.out / f"activity_{tag}_{name}.tcl", r.out / f"activity_{tag}_{name}_raw.tcl"
            r.info[f"activity_annotation_{tag}_{name}"] = activity_tcl(netlist, vcd, LIB["tt"],
                                                                       act_f, act_r)
            vcd.unlink()
            if tag == "layout":
                design = _design_tcl(d, LIB["tt"], netlist, top)
                cnl = nl
            else:
                design = (f"read_liberty {LIB['tt']}\nread_verilog {netlist}\nlink_design {top}\n"
                          f"create_clock -name clk -period {period} [get_ports clk]")
                cnl = NL.read_netlist(netlist, lib)
            tcl = LAYOUT_POWER_TCL.format(design=design, activity=act_f)
            (r.out / f"power_{tag}_{name}.tcl").write_text(tcl)
            _, text = r.sh(f"{STA} {r.out / f'power_{tag}_{name}.tcl'}",
                           f"power_{tag}_{name}.log")
            lost = len(re.findall(r"^Warning \d+: .* not found", text, re.M))
            if lost:
                r.failed_tests.append(f"power {tag} {name}: {lost} activity annotations not "
                                      f"applied (see power_{tag}_{name}.log)")
            groups = _power(text)
            by_class: dict[str, float] = {}
            inst_power[f"{tag}_{name}"] = _instance_power(text)
            for inst, p in inst_power[f"{tag}_{name}"].items():
                c = cnl.cells.get(inst)
                k = NL.cell_class(cnl, c) if c else "other"
                by_class[k] = by_class.get(k, 0.0) + p
            power[f"{tag}_{name}"] = {
                "total_uw": round(groups.get("Total", {}).get("total", 0), 3),
                "by_group_uw": {g: round(v["total"], 3) for g, v in groups.items() if v["total"]},
                "by_cell_class_uw": {k: round(v, 3) for k, v in sorted(by_class.items())}}
    r.info["layout_power"] = power
    for name, metric in [("op", "layout_power_uw"), ("idle", "layout_power_idle_uw")]:
        p = power.get(f"layout_{name}")
        if p:
            r.metrics[metric] = p["total_uw"]
            cls = p["by_cell_class_uw"]
            r.info[f"layout_clock_power_{name}_uw"] = round(
                cls.get("clock_buffer", 0) + cls.get("clock_gate", 0)
                + cls.get("clock_dummy_load", 0), 3)
    if "layout_op" in power and "prelayout_op" in power:
        r.info["layout_minus_prelayout_op_uw"] = round(
            power["layout_op"]["total_uw"] - power["prelayout_op"]["total_uw"], 3)

    # --- clock roots: what each gate's subtree costs (its buffers + the gate itself) --------
    root_rows = []
    for rt in roots:
        cells = rt["buffers"] + ([rt["driver"]] if rt["driver"] in nl.cells else [])
        row = {k: rt[k] for k in ("root", "enable", "flops", "child_gates", "depth",
                                  "delay_buffers", "dummy_loads", "buffer_area_um2",
                                  "buffer_types")}
        row["buffers"] = len(rt["buffers"])
        for name in names:
            ip = inst_power.get(f"layout_{name}", {})
            row[f"{name}_uw"] = round(sum(ip.get(c, 0.0) for c in cells), 3) if ip else None
        root_rows.append(row)
    root_rows.sort(key=lambda x: -(x.get("op_uw") or 0))
    r.info["layout_clock_roots_top"] = root_rows[:5]
    rcols = ["root", "enable", "flops", "child_gates", "buffers", "depth", "delay_buffers",
             "dummy_loads", "buffer_area_um2", "buffer_types"] + [f"{n}_uw" for n in names]
    md += ["## Clock roots (port and every clock gate), by running power", "",
           "Power: the root's clock buffers plus its gate, real-data scenario (T-PWR-3). "
           "Delay buffers: CTS latency balancing. Dummy loads: CTS capacitance balancing.", ""]
    md += _md_table(root_rows[:30], rcols)
    (r.out / "layout_fanout.md").write_text("\n".join(md) + "\n")
    (r.out / "layout_fanout.json").write_text(json.dumps(
        {"limit_ns": limit, "top": rows, "slowest": slow, "clock": clock,
         "clock_roots": root_rows}, indent=2))
    r.notes.append("layout: T-PWR-3 runs the TT top through the pins (128 slots x 2 clocks = "
                   "real frame rate), so it includes the pin glue; clock-tree nets use raw "
                   "toggles of the unit-delay simulation, data nets glitch-free sampling")


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
           "layout": lambda run: step_layout(run, args.quick),
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
