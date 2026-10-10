"""Summary tables of the fault-injection runs (test/bringup, both benches): per fault, and per
class.   python scripts/bringup/fault_table.py [results dir]"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# test -> fault class (the order of the tables)
CLASS = {
    "f_clean": "control", "f_short": "control", "t_clean": "control",
    "f_a_dp2": "latch row never written", "f_a_o1": "latch row never written",
    "f_a_sx": "latch row never written", "f_a_gclk": "latch enable (row clock gate) dead",
    "f_a_bit": "latch row bit stuck",
    "f_b_ld": "shared lifter", "f_b_la": "shared lifter", "f_b_sgdp": "staging register",
    "f_c_state": "rANS state upset", "f_c_rom": "rANS ROM", "f_c_wb": "divider write-back",
    "f_c_xl": "looped divider data (x_l)", "f_c_itv": "looped divider control (it_v)",
    "f_d_slot": "slot selector", "f_d_swap": "slot selector",
    "f_e_drop": "byte path", "f_e_corrupt": "byte path", "f_e_pin5": "output pin",
    "f_f_seqbit": "header / seq", "f_f_sequpset": "header / seq",
    "t_rst_sync": "reset synchroniser", "t_stb_sync": "input-sync flop",
    "t_frm_q": "input-sync flop", "t_hi_q": "input capture flop",
    "t_last_q": "registered output", "t_data_q": "registered output",
}


def main() -> int:
    d = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "test" / "results" / "bringup"
    res = {p.parent.name: json.loads(p.read_text()) for p in d.glob("*/result.json")}
    names = [n for n in CLASS if n in res] + sorted(set(res) - set(CLASS))
    print("| test | class | injected fault | tool's verdict | correct | precision |")
    print("|---|---|---|---|---|---|")
    for n in names:
        r = res[n]
        v = r["verdict"].replace("|", "/")
        if len(v) > 200:
            v = v[:197] + "..."
        print(f"| {n} | {CLASS.get(n, '?')} | {r['fault']} | {v} | "
              f"{'yes' if r['correct'] else 'NO'} | {r['precision']} |")
    print("\n| class | injected | diagnosed | confidence |")
    print("|---|---|---|---|")
    seen: dict = {}
    for n in names:
        seen.setdefault(CLASS.get(n, "?"), []).append(res[n])
    for c, rs in seen.items():
        conf = sorted({r.get("confidence") or "-" for r in rs})
        print(f"| {c} | {len(rs)} | {sum(r['correct'] for r in rs)} | {', '.join(conf)} |")
    ok = sum(r["correct"] for r in res.values())
    print(f"\n{ok}/{len(res)} correct")
    return 0 if ok == len(res) else 1


if __name__ == "__main__":
    sys.exit(main())
