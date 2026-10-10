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

# CVC: "... timing violation in <scope> ... $setuphold(..." ; Icarus has no timing checks
VIOL = re.compile(r"timing violation|\$(setuphold|setup|hold|recrem|recovery|removal|width|period)\b.*violat",
                  re.I)
CHECK = re.compile(r"\$(setuphold|setup|hold|recrem|recovery|removal|width|period)", re.I)
INST = re.compile(r"(tb\.user_project\.[\w.\\\[\]]+)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sim", required=True)
    ap.add_argument("--corner", required=True)
    ap.add_argument("--run", default="")
    ap.add_argument("--log", type=Path, required=True)
    ap.add_argument("--results", type=Path, required=True)
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
    sdf = [l for l in lines if re.search(r"sdf|SDF", l)][:15]
    if sdf:
        print("\nSDF messages (first 15):\n```\n" + "\n".join(sdf) + "\n```")


if __name__ == "__main__":
    main()
