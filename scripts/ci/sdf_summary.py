"""Job summary of an SDF gate-level run (.github/workflows/sdf_sim.yaml, T-GL-3).

Functional result (cocotb results.xml) and timing-check violations (simulator log) are
reported separately: a violation sets the flop's notifier (output X) but need not change a byte.
"""

from __future__ import annotations

import argparse
import collections
import re
import xml.etree.ElementTree as ET
from pathlib import Path

# any simulator message about a timing-check violation (Icarus runs no timing checks)
VIOL = re.compile(r"violation", re.I)
CHECK = re.compile(r"\$(setuphold|setup|hold|recrem|recovery|removal|width|period)", re.I)
INST = re.compile(r"(tb\.user_project\.[\w.\\\[\]]+)")
MSG = re.compile(r"(ERROR|WARN|INFORM)\*\* \[\d+\]")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sim", required=True)
    ap.add_argument("--corner", required=True)
    ap.add_argument("--run", default="")
    ap.add_argument("--log", type=Path, required=True)
    ap.add_argument("--results", type=Path, required=True)
    ap.add_argument("--sdf-log", type=Path)
    ap.add_argument("--selfcheck-log", type=Path)
    a = ap.parse_args()

    print(f"## T-GL-3 SDF gate level: {a.sim}, {a.corner}\n\n{a.run}\n")
    print("| test | result | sim time (ns) | wall (s) |\n|---|---|---|---|")
    if a.results.exists():
        for tc in ET.parse(a.results).getroot().iter("testcase"):
            res = ("FAIL" if tc.find("failure") is not None or tc.find("error") is not None
                   else "skip" if tc.find("skipped") is not None else "pass")
            print(f"| {tc.get('name')} | {res} | {tc.get('sim_time_ns', '')} | "
                  f"{float(tc.get('time', 0)):.0f} |")
    else:
        print("| (no results.xml) | FAIL | | |")

    lines = a.log.read_text(errors="replace").splitlines() if a.log.exists() else []
    viol = [l for l in lines if VIOL.search(l)]
    kinds = collections.Counter((CHECK.search(l).group(1).lower() if CHECK.search(l) else "?")
                                for l in viol)
    insts = collections.Counter(INST.search(l).group(1) if INST.search(l) else "?" for l in viol)
    print(f"\n**timing-check violations: {len(viol)}**"
          + ("" if a.sim != "icarus" else " (Icarus does not run timing checks)"))
    if viol:
        print("\n| check | count |\n|---|---|")
        for k, c in kinds.most_common():
            print(f"| {k} | {c} |")
        print("\n| instance | count |\n|---|---|")
        for k, c in insts.most_common(20):
            print(f"| `{k}` | {c} |")
        print("\nfirst violations:\n```")
        print("\n".join(viol[:15]))
        print("```")

    if a.selfcheck_log:
        st = (a.selfcheck_log.read_text(errors="replace").splitlines()
              if a.selfcheck_log.exists() else [])
        sv = [l for l in st if VIOL.search(l)]
        print(f"\ntiming-check self-test (test_sdf: cfg_en swept through the clock edge): "
              f"{len(sv)} violations, " + ("checks are live" if sv else "**checks NOT live**"))
        if sv:
            print("```\n" + "\n".join(sv[:5]) + "\n```")

    dly = [l.split("delay: ", 1)[1] for l in lines if "SDF clk->out delay" in l]
    mins = [l for l in dly if l.startswith("new min")]
    maxs = [l for l in dly if l.startswith("new max")]
    print(f"\nclock pin -> output pin delay (tb probe): {mins[-1] if mins else '-'}, "
          f"{maxs[-1] if maxs else '-'} (about 0 = delays not annotated)")

    sdf = [l for l in lines if re.search(r"sdf|SDF", l) and "clk->out" not in l]
    if a.sdf_log and a.sdf_log.exists():
        sdf += a.sdf_log.read_text(errors="replace").splitlines()
    msgs = collections.Counter(m.group(0) for l in sdf for m in [MSG.search(l)] if m)
    print("\nSDF annotation messages: "
          + (", ".join(f"{k} x{c}" for k, c in sorted(msgs.items())) or "none"))
    errs = [l for l in sdf if "ERROR" in l or "rror" in l][:10]
    if errs:
        print("```\n" + "\n".join(errs) + "\n```")


if __name__ == "__main__":
    main()
