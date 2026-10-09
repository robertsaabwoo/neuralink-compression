"""One table from the nightly artifacts (.github/workflows/nightly.yaml).

  python scripts/ci/nightly_aggregate.py <artifact dir> [--csv out.csv]

Per sweep entry: LibreLane result (full flow: final metrics present), routed-netlist simulation
(bit-exact on the power scenarios), power running/idle with the core / TT pins / clock trunk split,
clock tree, hold and setup slack, hold buffers, utilisation, cell area, slews. Per RTL job: seed
results. Power is on synthetic data in CI (compare entries with each other, not with real-data runs).
"""

import csv
import json
import sys
from pathlib import Path


def f(v, n=2):
    return "-" if v is None else (f"{v:.{n}f}" if isinstance(v, float) else str(v))


def sweep_row(d: Path) -> dict | None:
    entry = next(d.rglob("entry.json"), None)
    if entry is None:
        return None
    e = json.loads(entry.read_text())
    if "filter" in e:
        return None
    met = next((p for p in d.rglob("metrics.json") if p.parent.name == "latest"), None)
    final = next((p for p in d.rglob("final/metrics.json")), None)
    row = {"name": e["name"], "ref": e["ref"], "flow": e.get("flow"), "noid": bool(e.get("noid")),
           "config": json.dumps(e.get("config") or {}), "tiles": e.get("tiles") or "",
           "librelane": "ok" if (final is not None or e.get("flow") == "preview") else "FAILED"}
    if met is None:
        row["sim"] = "no report"
        return row
    m = json.loads(met.read_text())
    x, i = m["metrics"], m["info"]
    blk = i.get("layout_power_by_block_uw", {})
    ct = i.get("layout_clock_tree", {})
    ws = i.get("layout_sta_ws", {})
    bad = [t for t in m.get("failed_tests", []) if "power scenario" in t or "crashed" in t]
    row.update({
        "sim": "ok" if not bad else "FAIL",
        "op_uw": x.get("layout_power_uw"), "idle_uw": x.get("layout_power_idle_uw"),
        "core_op": blk.get("op", {}).get("core"), "core_idle": blk.get("idle", {}).get("core"),
        "pins_idle": blk.get("idle", {}).get("pins"), "trunk_op": blk.get("op", {}).get("clock_trunk"),
        "op_quiet": i.get("layout_power", {}).get("layout_op_quiet", {}).get("total_uw"),
        "idle_quiet": i.get("layout_power", {}).get("layout_idle_quiet", {}).get("total_uw"),
        "clk_bufs": ct.get("clock_buffers"), "delay_bufs": ct.get("latency_delay_buffers"),
        "hold_ff": ws.get("ff", {}).get("hold"), "hold_ll": i.get("layout_librelane", {}).get("hold_ws_ns"),
        "hold_bufs": i.get("layout_hold_buffers"), "setup_ss": ws.get("ss", {}).get("setup"),
        "util": x.get("layout_utilisation"), "stdcell": i.get("layout_librelane", {}).get("stdcell_area_um2"),
        "slew_ss": x.get("layout_slew_violations_ss"), "kind": i.get("layout_kind")})
    return row


def rtl_row(d: Path) -> dict | None:
    s = next(d.rglob("summary.txt"), None)
    if s is None:
        return None
    lines = s.read_text().split("\n")
    return {"job": d.name.removeprefix("nightly-rtl-"), "pass": sum("pass" in l for l in lines),
            "fail": sum("FAIL" in l for l in lines),
            "failed": " ".join(l.split(":")[0] for l in lines if "FAIL" in l)}


def main() -> None:
    art = Path(sys.argv[1])
    rows = [r for d in sorted(art.iterdir()) if d.is_dir() for r in [sweep_row(d)] if r]
    rtl = [r for d in sorted(art.iterdir()) if d.is_dir() and d.name.startswith("nightly-rtl-")
           for r in [rtl_row(d)] if r]
    rows.sort(key=lambda r: (r.get("sim") != "ok", r.get("op_uw") or 1e9))
    cols = ["name", "ref", "flow", "noid", "config", "tiles", "librelane", "sim", "op_uw", "idle_uw",
            "core_op", "core_idle", "pins_idle", "trunk_op", "op_quiet", "idle_quiet", "clk_bufs",
            "delay_bufs", "hold_ff", "hold_ll", "hold_bufs", "setup_ss", "util", "stdcell", "slew_ss"]
    if "--csv" in sys.argv:
        with open(sys.argv[sys.argv.index("--csv") + 1], "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
    print("## Nightly sweep (synthetic data in CI; best running power first)\n")
    print("| " + " | ".join(cols) + " |")
    print("|" + "---|" * len(cols))
    for r in rows:
        print("| " + " | ".join(f(r.get(c)) for c in cols) + " |")
    print("\n## RTL regressions\n\n| job | pass | fail | failed seeds |\n|---|---|---|---|")
    for r in rtl:
        print(f"| {r['job']} | {r['pass']} | {r['fail']} | {r['failed']} |")


if __name__ == "__main__":
    main()
