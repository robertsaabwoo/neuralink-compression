"""Reverse lookup: report terms -> RTL instance names, generated from the RTL sources.

Parses src/ (no hand-kept table): the instance chain from the Tiny Tapeout top down to
nlc_lossy, the per-channel register rows of nlc_lossy's `g_ch` generate loop, the rANS state
rows of nlc_rans' `g_st` loop, the lifting / quantiser / serialiser lines. Every entry gives
the RTL hierarchical path, the clock-gate cell it implies in the synthesised netlist
(nlc_greg -> u_icg -> sky130 dlclkp u_cg) and the source line.

  python scripts/bringup/rtl_map.py            print the table
  python scripts/bringup/rtl_map.py --json     as JSON
"""

from __future__ import annotations

import json
import re
import sys
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"

# what each per-channel row holds (for the report); the field names come from the RTL
ROW_ROLE = {
    "e1": "level-1 lifter: last even sample", "o1": "level-1 lifter: pending odd sample",
    "dp1": "level-1 lifter: previous detail d1[i-1]",
    "e2": "level-2 lifter: last even input (a1)", "o2": "level-2 lifter: pending odd input (a1)",
    "dp2": "level-2 lifter: previous detail d2[i-1]",
    "e3": "level-3 lifter: last even input (a2)", "o3": "level-3 lifter: pending odd input (a2)",
    "dp3": "level-3 lifter: previous detail d3[i-1]",
    "qa": "previous quantised a3 (a3 delta reference)",
    "b0": "burst buffer slot 0 (quantised d1)", "b1": "burst buffer slot 1 (quantised d2)",
    "b2": "burst buffer slot 2 (quantised d3)", "b3": "burst buffer slot 3 (a3 delta)",
}
SIG_ROLE = {
    "x0": "centred sample smp_data (slot selector output / input pins)",
    "d1": "u_l1 detail output", "a1": "u_l1 approximation output",
    "d2": "u_l2 detail output", "a2": "u_l2 approximation output",
    "d3": "u_l3 detail output", "a3": "u_l3 approximation output",
    "q1": "quantiser d1 (>>3)", "q2": "quantiser d2 (>>2)", "q3": "quantiser d3 (>>2)",
    "qa3": "quantiser a3 (>>1)", "da3": "a3 delta subtractor",
    "hv": "issuer read mux (symbol value into the coder)",
}


def _lines(name: str) -> list[str]:
    return (SRC / name).read_text().splitlines()


def _find(name: str, pat: str) -> tuple[int, re.Match] | None:
    for i, line in enumerate(_lines(name), 1):
        m = re.search(pat, line)
        if m:
            return i, m
    return None


def _inst(parent_file: str, module: str) -> str:
    """Instance name of `module` inside parent_file (first match)."""
    hit = _find(parent_file, rf"^\s*{module}\b.*?\b(u_\w+|core)\s*\(")
    if hit:
        return hit[1].group(1)
    # instance on the line after the parameter list
    lines = _lines(parent_file)
    for i, line in enumerate(lines):
        if re.match(rf"^\s*{module}\b", line):
            for nxt in lines[i:i + 4]:
                m = re.search(r"\)\s*(\w+)\s*\(", nxt) or re.search(r"^\s*(\w+)\s*\($", nxt)
                if m:
                    return m.group(1)
    raise RuntimeError(f"no {module} instance in {parent_file}")


@lru_cache(maxsize=1)
def build() -> dict:
    top_m = _find("project.v", r"^module\s+(\w+)")[1].group(1)
    core = _inst("project.v", "nlc_core")
    enc = _inst("nlc_core.v", "nlc_encoder")
    ly = _inst("nlc_encoder.v", "nlc_lossy")
    rn = _inst("nlc_lossy.sv", "nlc_rans")
    sel = _inst("nlc_core.v", "nlc_slot_sel")
    cfg = _inst("nlc_core.v", "nlc_cfg")
    fifo = _inst("nlc_core.v", "nlc_out_fifo")
    p_ly = f"{core}.{enc}.{ly}"
    out: dict = {"top_module": top_m, "lossy": p_ly, "rows": {}, "state_rows": {}, "signals": {},
                 "blocks": {}}

    # per-channel rows: nlc_greg instances inside `for (gc ...) begin : g_ch`
    lines = _lines("nlc_lossy.sv")
    loop = None
    for i, line in enumerate(lines, 1):
        m = re.search(r"begin\s*:\s*(g_\w+)", line)
        if m and "gc" in line:
            loop = m.group(1)
        m = re.search(r"nlc_greg\s*#\(\.W\((\w+)\)\)\s*(u_\w+)\s*\(.*\.en\(([^)]*\)?)\),\s*"
                      r"\.d\(([^)]*)\),\s*\.q\((\w+)\[gc\]\)", line)
        if m and loop == "g_ch":
            w, inst, en, d, q = m.groups()
            field = {"qa_prev": "qa", "bq0": "b0", "bq1": "b1", "bq2": "b2", "bq3": "b3"}.get(q, q)
            out["rows"][field] = {"inst": inst, "array": q, "width_param": w, "d": d.strip(),
                                  "en": en.strip(), "line": i, "loop": loop,
                                  "role": ROW_ROLE.get(field, ""),
                                  "rtl": f"{p_ly}.{loop}[{{c}}].{inst}",
                                  "netlist_gate": f"{p_ly}.{loop}[{{c}}].{inst}.u_icg.u_cg"}
    # rANS state rows
    hit = None
    for i, line in enumerate(_lines("nlc_rans.sv"), 1):
        m = re.search(r"begin\s*:\s*(g_st)", line)
        if m:
            hit = m.group(1)
        m = re.search(r"nlc_greg\s*#\(\.W\((\w+)\)\)\s*(u_\w+)", line)
        if m and hit:
            out["state_rows"] = {"inst": m.group(2), "line": i, "loop": hit,
                                 "rtl": f"{p_ly}.{rn}.{hit}[{{c}}].{m.group(2)}",
                                 "netlist_gate": f"{p_ly}.{rn}.{hit}[{{c}}].{m.group(2)}.u_icg.u_cg"}
            break
    # shared datapath signals
    for name in ("u_l1", "u_l2", "u_l3"):
        i, _ = _find("nlc_lossy.sv", rf"nlc_lift53\b.*\b{name}\s*\(")
        lvl = name[-1]
        for s in ("d", "a"):
            out["signals"][f"{s}{lvl}"] = {"rtl": f"{p_ly}.{name}.{s}", "line": i,
                                           "file": "src/nlc_lift53.sv (instance in nlc_lossy.sv)",
                                           "role": SIG_ROLE[f"{s}{lvl}"]}
    for s in ("q1", "q2", "q3", "qa3", "da3", "x0", "hv"):
        hit = _find("nlc_lossy.sv", rf"assign\s+{s}\s*=") or _find("nlc_lossy.sv", rf"\b{s}\s*=")
        out["signals"][s] = {"rtl": f"{p_ly}.{s}", "line": hit[0] if hit else None,
                             "file": "src/nlc_lossy.sv", "role": SIG_ROLE[s]}

    def blk(key, path, file, pat, role):
        h = _find(file, pat)
        out["blocks"][key] = {"rtl": path, "file": f"src/{file}", "line": h[0] if h else None,
                              "role": role}
    blk("rans_coder", f"{p_ly}.{rn}", "nlc_rans.sv", r"^module", "rANS coder (shared, II=1)")
    blk("rans_rom", f"{p_ly}.{rn}.u_rom", "nlc_rans.sv", r"nlc_lossy_rom u_rom", "probability tables")
    blk("rans_flush", f"{p_ly}.{rn} (f_ch/f_b, f_sh)", "nlc_rans.sv", r"assign f_sh",
        "final-state flush mux")
    blk("issuer", f"{p_ly} (rr, kk, j, pending)", "nlc_lossy.sv", r"Issuer:",
        "issuer: burst drain in coding order")
    blk("serialiser", f"{p_ly} (seq, need_hdr, bi)", "nlc_lossy.sv", r"Serialiser:",
        "header byte + coder word serialiser")
    blk("seq", f"{p_ly}.seq", "nlc_lossy.sv", r"logic \[5:0\] seq", "packet sequence counter")
    blk("schedule", f"{p_ly} (n, blk, pk)", "nlc_lossy.sv", r"Global schedule",
        "frame / block counters")
    blk("slot_sel", f"{core}.{sel}", "nlc_slot_sel.v", r"^module", "slot selector")
    blk("slot_sel_smp", f"{core}.{sel}.u_smp", "nlc_slot_sel.v", r"nlc_greg.*u_smp",
        "slot selector sample register {smp_data, smp_ch}")
    blk("cfg_slots", f"{core}.{cfg}.slot_r", "nlc_cfg.v", r"slot_r\s*\[", "slot config registers")
    blk("out_fifo", f"{core}.{fifo}", "nlc_out_fifo.v", r"^module", "output byte FIFO")
    blk("pins_out", f"{top_m}: uo_out / uio_out[7:6]", "project.v", r"assign uo_out",
        "output pins (m_data, m_valid, m_last)")
    blk("pins_in", f"{top_m}: ui_q / hi_q pad registers", "project.v", r"reg \[7:0\] ui_q",
        "input pin registers (sample)")
    blk("abort", f"{core}.{enc} (tok, in_pkt)", "nlc_encoder.v", r"wire\s+tok_set",
        "abort-token logic")
    return out


def row(field: str, c: int) -> dict:
    r = build()["rows"][field]
    return {"term": f"row {field} of channel {c}", "rtl": r["rtl"].format(c=c),
            "netlist_gate": r["netlist_gate"].format(c=c), "src": f"src/nlc_lossy.sv:{r['line']}",
            "role": r["role"]}


def state_row(c: int) -> dict:
    r = build()["state_rows"]
    return {"term": f"rANS state row of channel {c}", "rtl": r["rtl"].format(c=c),
            "netlist_gate": r["netlist_gate"].format(c=c), "src": f"src/nlc_rans.sv:{r['line']}",
            "role": "22-bit rANS state of the channel"}


def signal(name: str) -> dict:
    s = build()["signals"][name]
    return {"term": f"shared signal {name}", "rtl": s["rtl"], "src": f"{s['file']}:{s['line']}",
            "role": s["role"]}


def block(key: str) -> dict:
    b = build()["blocks"][key]
    return {"term": key, "rtl": b["rtl"], "src": f"{b['file']}:{b['line']}", "role": b["role"]}


def table(n_ch: int = 8) -> str:
    m = build()
    out = [f"top {m['top_module']}, lossy core {m['lossy']}", "",
           "per-channel rows (one clock gate per field per channel), c = 0.."
           f"{n_ch - 1}:"]
    for f, r in m["rows"].items():
        out.append(f"  row {f:4s} {r['rtl']:44s} gate {r['netlist_gate'].split('.', 1)[1]:46s}"
                   f" src/nlc_lossy.sv:{r['line']:<4d} {r['role']}")
    s = m["state_rows"]
    out.append(f"  rANS state  {s['rtl']}   gate ...{s['netlist_gate'][-11:]}   "
               f"src/nlc_rans.sv:{s['line']}")
    out += ["", "shared signals:"]
    for k, s in m["signals"].items():
        out.append(f"  {k:4s} {s['rtl']:40s} {s['file']}:{s['line']}  {s['role']}")
    out += ["", "blocks:"]
    for k, b in m["blocks"].items():
        out.append(f"  {k:13s} {b['rtl']:48s} {b['file']}:{b['line']}  {b['role']}")
    return "\n".join(out)


if __name__ == "__main__":
    if "--json" in sys.argv:
        print(json.dumps(build(), indent=1))
    else:
        print(table())
