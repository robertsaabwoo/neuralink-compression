"""Markdown summary of a cts_preview run (GitHub job summary): CTS settings, clock tree, power,
hold, plus the most expensive clock roots. Exit status 1 if the routed-netlist simulation
failed (bytes differ from the model) or the layout step crashed.

  python scripts/ci/preview_summary.py >> "$GITHUB_STEP_SUMMARY"
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REP = ROOT / "reports" / "latest"


def main() -> int:
    m = json.loads((REP / "metrics.json").read_text())
    x, i = m["metrics"], m["info"]
    cfg = json.loads((ROOT / "src" / "config.json").read_text())
    cts = {k: v for k, v in cfg.items() if k.startswith("CTS_")} or "TT defaults"
    ct = i.get("layout_clock_tree", {})
    fr = ct.get("free_running", {})
    ws = i.get("layout_sta_ws", {})
    out = [
        f"## CTS preview: {i.get('layout_source', {}).get('commit', '?')[:7]} "
        f"({i.get('layout_kind')}, {i.get('layout_power_data')} data)", "",
        f"CTS settings: `{json.dumps(cts)}`", "",
        "| metric | value |", "|---|---|",
        f"| power running / idle (uW) | {x.get('layout_power_uw')} / {x.get('layout_power_idle_uw')} |",
        f"| clock power running / idle (uW) | {i.get('layout_clock_power_op_uw')} / "
        f"{i.get('layout_clock_power_idle_uw')} |",
        f"| clock buffers (area um^2) | {ct.get('clock_buffers')} ({ct.get('clock_buffer_area_um2')}) |",
        f"| buffer types | {ct.get('buffer_types')} |",
        f"| latency delay buffers | {ct.get('latency_delay_buffers')} |",
        f"| free-running trunk: buffers / depth / flops | {fr.get('buffers')} / {fr.get('depth')} / "
        f"{fr.get('flops')} |",
        f"| hold slack ss / tt / ff (ns) | " + " / ".join(
            str(ws.get(c, {}).get("hold")) for c in ("ss", "tt", "ff")) + " |",
        f"| hold buffers | {i.get('layout_hold_buffers')} |",
        f"| setup slack ss (ns) | {ws.get('ss', {}).get('setup')} |",
        f"| utilisation | {round(x.get('layout_utilisation') or 0, 3)} |",
        f"| slew violations ss / tt | {x.get('layout_slew_violations_ss')} / "
        f"{x.get('layout_slew_violations_tt')} |", ""]
    fan = REP / "layout_fanout.md"
    if fan.exists():
        text = fan.read_text()
        roots = text[text.find("## Clock roots"):].splitlines()
        out += roots[:16] + [""]
    out += [f"- {n}" for n in m.get("notes", [])]
    if m.get("failed_tests"):
        out += ["", "### Failed", ""] + [f"- {t}" for t in m["failed_tests"]]
    print("\n".join(out))
    bad = [t for t in m.get("failed_tests", []) if re.search(r"layout|crashed", t)]
    return 1 if bad or "layout_power_uw" not in x else 0


if __name__ == "__main__":
    sys.exit(main())
