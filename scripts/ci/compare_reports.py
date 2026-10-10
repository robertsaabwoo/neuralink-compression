"""Compare layout-step reports (T-PWR-3 / T-FAN-1) side by side, one row each:

  python scripts/ci/compare_reports.py base=reports/<stamp> exp=<dir with metrics.json> ...

Directories are absolute or relative to the repo (e.g. unpacked cts_preview_report artifacts).
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
rows = []
for arg in sys.argv[1:]:
    name, d = arg.split("=", 1)
    m = json.loads((ROOT / d / "metrics.json").read_text())
    i, x = m["info"], m["metrics"]
    ct = i["layout_clock_tree"]
    ll = i["layout_librelane"]
    pw = i["layout_power"]
    rows.append({
        "exp": name,
        "op uW": x.get("layout_power_uw"),
        "idle uW": x.get("layout_power_idle_uw"),
        "clock op/idle uW": f'{i.get("layout_clock_power_op_uw")} / {i.get("layout_clock_power_idle_uw")}',
        "clk bufs": ct["clock_buffers"],
        "clk buf um2": ct["clock_buffer_area_um2"],
        "delay bufs": ct["latency_delay_buffers"],
        "trunk bufs/depth": f'{ct["free_running"]["buffers"]} / {ct["free_running"]["depth"]}',
        "hold ws ns": round(x.get("layout_hold_slack_ns") or 0, 3),
        "hold bufs": i.get("layout_hold_buffers"),
        "setup ws ns": round(ll.get("setup_ws_ns") or 0, 1),
        "util": round(x.get("layout_utilisation") or 0, 3),
        "stdcell um2": ll.get("stdcell_area_um2"),
        "slew viol ss/tt": f'{x.get("layout_slew_violations_ss")} / {x.get("layout_slew_violations_tt")}',
        "clock max slew ss": i.get("layout_clock_max_slew_ss_ns"),
        "types": ", ".join(f"{k} {v}" for k, v in ct["buffer_types"].items()),
        "failed": len(m["failed_tests"]),
    })
cols = list(rows[0])
print("| " + " | ".join(cols) + " |")
print("|" + "---|" * len(cols))
for r in rows:
    print("| " + " | ".join(str(r[c]) for c in cols) + " |")
