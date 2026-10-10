"""Summary table of the fault-injection run (test/bringup): injected fault | diagnosis |
correct? | precision.   python scripts/bringup/fault_table.py [results dir]"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ORDER = ["f_clean", "f_stall", "f_a_dp2", "f_a_o1", "f_a_b3", "f_a_gclk", "f_b_d2", "f_b_a1",
         "f_c_state", "f_c_rom", "f_c_wb", "f_d_slot", "f_d_swap", "f_e_drop", "f_e_corrupt", "f_e_pin5",
         "f_f_seqbit", "f_f_sequpset"]


def main() -> int:
    d = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "test" / "results" / "bringup"
    res = {p.parent.name: json.loads(p.read_text()) for p in d.glob("*/result.json")}
    names = [n for n in ORDER if n in res] + sorted(set(res) - set(ORDER))
    print("| test | injected fault | tool's verdict | correct | precision |")
    print("|---|---|---|---|---|")
    for n in names:
        r = res[n]
        v = r["verdict"].replace("|", "/")
        if len(v) > 260:
            v = v[:257] + "..."
        print(f"| {n} | {r['fault']} | {v} | {'yes' if r['correct'] else 'NO'} | "
              f"{r['precision']} |")
    ok = sum(r["correct"] for r in res.values())
    print(f"\n{ok}/{len(res)} correct")
    return 0 if ok == len(res) else 1


if __name__ == "__main__":
    sys.exit(main())
