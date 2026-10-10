"""Reverse lookup: report terms -> RTL instance names, generated from the RTL sources.

Parses src/ (no hand-kept table): the instance chain from the Tiny Tapeout top down to
nlc_lossy, the per-channel latch rows of nlc_lossy's `g_ch` generate loop, the rANS state
rows of nlc_rans' `g_st` loop, the shared lifter, staging registers, divider loop register,
and the pin front end of project.v. Every row entry gives the RTL hierarchical path, the
clock-gate cell and latch cells it implies in the synthesised netlist (nlc_rreg -> g_l.u_r
(nlc_lreg) -> u_icg.u_cg = sky130 dlclkp, g_b[i].u_l = dlxtp) and the source line.

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
    "sx": "the channel's sample, until the issuer lifts it",
    "e1": "level-1 lifting: last even sample", "o1": "level-1 lifting: pending odd sample",
    "dp1": "level-1 lifting: previous detail d1[i-1]",
    "e2": "level-2 lifting: last even input (a1)", "o2": "level-2 lifting: pending odd input (a1)",
    "dp2": "level-2 lifting: previous detail d2[i-1]",
    "e3": "level-3 lifting: last even input (a2)", "o3": "level-3 lifting: pending odd input (a2)",
    "dp3": "level-3 lifting: previous detail d3[i-1]",
    "qa": "previous quantised a3 (a3 delta reference)",
}
SIG_ROLE = {
    "x0": "centred sample: input pins ui_in/uio[1:0] -> ui_q/hi_q -> slot selector u_smp "
          "(x0[9] = inverted sample bit 9)",
    "sg_sx": "sample staging register (u_sgx), feeds every sx row",
    "lx": "shared lifter input x (sample / a1 / a2)", "le": "shared lifter input e",
    "lo": "shared lifter input o", "ldp": "shared lifter input dp",
    "la": "shared lifter approximation output (a1/a2/a3)",
    "ld": "shared lifter detail output (d1/d2/d3)",
    "q1": "quantiser d1 (>>3)", "q2": "quantiser d2 (>>2)", "q3": "quantiser d3 (>>2)",
    "qa3": "quantiser a3 (>>1)", "da3": "a3 delta subtractor",
    "xr": "inter-level register u_x (a1 -> level 2, a2 -> level 3, delta a3 out)",
    "hv": "issuer value mux (symbol value into the coder)",
    "sg_xq": "staging register: level-1 input / quantised a3 (e1, o1, qa rows)",
    "sg_e2": "staging register: level-2 input (e2, o2 rows)",
    "sg_e3": "staging register: level-3 input (e3, o3 rows)",
    "sg_dp": "staging register: detail (dp1, dp2, dp3 rows)",
}
SIG_INST = {"lx": "u_lift.x", "le": "u_lift.e", "lo": "u_lift.o", "ldp": "u_lift.dp",
            "la": "u_lift.a", "ld": "u_lift.d", "xr": "u_x.q", "sg_sx": "g_sg.u_sgx.q",
            "sg_xq": "g_sg.u_sg.q[47:36]", "sg_e2": "g_sg.u_sg.q[35:25]",
            "sg_e3": "g_sg.u_sg.q[24:13]", "sg_dp": "g_sg.u_sg.q[12:0]"}


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
    lines = _lines(parent_file)
    for i, line in enumerate(lines):
        if re.match(rf"^\s*{module}\b", line):
            for nxt in lines[i:i + 4]:
                m = re.search(r"\)\s*(\w+)\s*\(", nxt) or re.search(r"^\s*(\w+)\s*\($", nxt)
                if m:
                    return m.group(1)
    raise RuntimeError(f"no {module} instance in {parent_file}")


def _latch_path(inst: str) -> tuple[str, str]:
    """nlc_rreg -> (clock gate cell, latch cells) under LATCH_ROWS = 1."""
    return f"{inst}.g_l.u_r.u_icg.u_cg", f"{inst}.g_l.u_r.g_b[*].u_l"


@lru_cache(maxsize=1)
def build() -> dict:
    top_m = _find("project.v", r"^module\s+(\w+)")[1].group(1)
    core = _inst("project.v", "nlc_core")
    enc = _inst("nlc_core.v", "nlc_encoder")
    ly = _inst("nlc_encoder.v", "nlc_lossy")
    rn = _inst("nlc_lossy.sv", "nlc_rans")
    sel = _inst("nlc_core.v", "nlc_slot_sel")
    cfg = _inst("nlc_core.v", "nlc_cfg")
    p_ly = f"{core}.{enc}.{ly}"
    out: dict = {"top_module": top_m, "lossy": p_ly, "rows": {}, "state_rows": {}, "signals": {},
                 "blocks": {}}

    # per-channel rows: nlc_rreg instances inside `for (gc ...) begin : g_ch`
    loop = None
    for i, line in enumerate(_lines("nlc_lossy.sv"), 1):
        m = re.search(r"begin\s*:\s*(g_\w+)", line)
        if m and "gc" in line:
            loop = m.group(1)
        m = re.search(r"nlc_rreg\s*#\(\.W\((\w+)\).*?\)\s*(u_\w+)\s*\(.*\.en\((.*?)\),\s*"
                      r"\.d\(([^)]*)\),\s*\.q\((\w+)\[gc\]\)", line)
        if m and loop == "g_ch":
            w, inst, en, d, q = m.groups()
            field = {"qa_prev": "qa"}.get(q, q)
            rtl = f"{p_ly}.{loop}[{{c}}].{inst}"
            gate, latches = _latch_path(rtl)
            out["rows"][field] = {"inst": inst, "array": q, "width": int(w), "d": d.strip(),
                                  "en": en.strip(), "line": i, "loop": loop,
                                  "role": ROW_ROLE.get(field, ""), "rtl": rtl,
                                  "netlist_gate": gate, "netlist_latches": latches}
    # rANS state rows
    hit = None
    for i, line in enumerate(_lines("nlc_rans.sv"), 1):
        m = re.search(r"begin\s*:\s*(g_st)", line)
        if m:
            hit = m.group(1)
        m = re.search(r"nlc_rreg\s*#\(.*\)\s*(u_\w+)\s*\(", line)
        if m and hit:
            rtl = f"{p_ly}.{rn}.{hit}[{{c}}].{m.group(1)}"
            gate, latches = _latch_path(rtl)
            out["state_rows"] = {"inst": m.group(1), "line": i, "loop": hit, "rtl": rtl,
                                 "netlist_gate": gate, "netlist_latches": latches}
            break
    # shared datapath nets
    i_lift, _ = _find("nlc_lossy.sv", r"nlc_lift53\b.*\bu_lift\s*\(")
    for s, role in SIG_ROLE.items():
        if s in SIG_INST:
            inst = SIG_INST[s]
            if inst.startswith("u_lift"):
                line = i_lift
            elif inst.startswith("u_x"):
                line = _find("nlc_lossy.sv", r"nlc_greg.*\bu_x\b")[0]
            elif "u_sgx" in inst:
                line = _find("nlc_lossy.sv", r"\bu_sgx\b")[0]
            else:
                line = _find("nlc_lossy.sv", r"\bu_sg\b")[0]
            out["signals"][s] = {"rtl": f"{p_ly}.{inst}", "line": line,
                                 "file": "src/nlc_lossy.sv", "role": role}
        else:
            h = _find("nlc_lossy.sv", rf"assign\s+{s}\s*=") or _find("nlc_lossy.sv",
                                                                       rf"\b{s}\s*=")
            out["signals"][s] = {"rtl": f"{p_ly}.{s}", "line": h[0] if h else None,
                                 "file": "src/nlc_lossy.sv", "role": role}

    def blk(key, path, file, pat, role):
        h = _find(file, pat)
        if h is None:
            raise RuntimeError(f"rtl_map: '{pat}' not found in src/{file} (RTL changed?)")
        out["blocks"][key] = {"rtl": path, "file": f"src/{file}", "line": h[0], "role": role}
    pr = f"{p_ly}.{rn}"
    blk("rans_coder", pr, "nlc_rans.sv", r"^module", "rANS coder (shared)")
    blk("rans_rom", f"{pr}.u_rom", "nlc_rans.sv", r"nlc_lossy_rom u_rom", "probability tables")
    blk("rans_div", f"{pr} (l_rem/l_dq steps, wb_x)", "nlc_rans.sv", r"assign wb_x",
        "divider steps + write-back adder")
    blk("rans_xl", f"{pr}.g_loop.u_xl", "nlc_rans.sv", r"nlc_greg.*\bu_xl\b",
        "divider loop register x_l (partial remainder/quotient; stages the state write)")
    blk("rans_ctl", f"{pr} (it_v, it_c, fire, s_tready)", "nlc_rans.sv",
        r"logic\s+it_v", "divider sequencer / coder handshake")
    blk("rans_flush", f"{pr} (f_ch/f_b, f_sh)", "nlc_rans.sv", r"assign f_sh",
        "final-state flush mux")
    blk("issuer", f"{p_ly} (rr, kk, j, pending)", "nlc_lossy.sv", r"Issuer \+ wavelet",
        "issuer: symbols in coding order, drives the shared lifter")
    blk("serialiser", f"{p_ly} (seq, need_hdr, bi)", "nlc_lossy.sv", r"Serialiser:",
        "header byte + coder word serialiser")
    blk("seq", f"{p_ly}.seq", "nlc_lossy.sv", r"logic \[5:0\] seq", "packet sequence counter")
    blk("schedule", f"{p_ly} (n, blk, pk)", "nlc_lossy.sv", r"Global schedule",
        "frame / block counters")
    blk("overflow", f"{p_ly}.overflow -> {top_m}.ovf_q -> uio[4]", "nlc_lossy.sv",
        r"sticky: an abort, coder", "sticky overflow (coder a frame behind)")
    blk("slot_sel", f"{core}.{sel}", "nlc_slot_sel.v", r"^module", "slot selector")
    blk("slot_sel_smp", f"{core}.{sel}.u_smp", "nlc_slot_sel.v", r"nlc_greg.*u_smp",
        "slot selector sample register {smp_data, smp_ch}")
    blk("slot_sel_run", f"{core}.{sel} (running, sel_idx)", "nlc_slot_sel.v",
        r"reg\s+running", "slot selector: started by the first s_frame")
    blk("cfg_slots", f"{core}.{cfg}.g_slot[*].u_slot", "nlc_cfg.v", r"nlc_greg.*u_slot",
        "slot config registers")
    blk("cfg_ctrl", f"{core}.{cfg} (enable, n_sel)", "nlc_cfg.v", r"enable <= cfg_data",
        "CTRL.enable / N_SEL registers")
    blk("core_gate", f"{core}.u_cg_core", "nlc_core.v", r"nlc_icg u_cg_core",
        "clock gate in front of the whole core")
    blk("abort", f"{core}.{enc} (tok, in_pkt)", "nlc_encoder.v", r"wire\s+tok_set",
        "abort-token logic")
    blk("rst_sync", f"{top_m}.rst_q", "project.v", r"reg \[1:0\] rst_q",
        "reset synchroniser (2 flops, the only reset inside the chip)")
    blk("stb_sync", f"{top_m}.stb_q", "project.v", r"reg\s+\[2:0\] stb_q",
        "s_strobe synchroniser + edge detect (uio[2])")
    blk("cfg_sync", f"{top_m}.cfg_q", "project.v", r"reg\s+\[1:0\] cfg_q",
        "cfg_en synchroniser (uio[5])")
    blk("frm_sync", f"{top_m}.frm_q / frame_q", "project.v", r"reg\s+frm_q",
        "s_frame capture (uio[3]): frm_q with stb_q[0], frame_q at R1")
    blk("pins_in", f"{top_m}.ui_q / hi_q", "project.v", r"reg\s+\[7:0\] ui_q",
        "input capture registers (sample, config byte)")
    blk("cfg_port", f"{top_m} (have_addr, cfg_we, u_cfg_addr, u_cfg_data)", "project.v",
        r"reg\s+have_addr", "config byte port (address, data)")
    blk("readback", f"{core}.{cfg}.rdata -> {top_m}.u_out", "nlc_cfg.v", r"assign rdata",
        "config readback mux")
    blk("out_reg", f"{top_m}.u_out (data_q), valid_q, last_q", "project.v",
        r"nlc_greg #\(\.W\(8\)\) u_out", "output register stage (+1 clock)")
    blk("pins_out", f"{top_m}: uo_out / uio_out[7:6]", "project.v", r"assign uo_out",
        "output pins (m_data, m_valid, m_last)")
    return out


def row(field: str, c: int) -> dict:
    r = build()["rows"][field]
    return {"term": f"row {field} of channel {c}", "rtl": r["rtl"].format(c=c),
            "netlist_gate": r["netlist_gate"].format(c=c),
            "netlist_latches": r["netlist_latches"].format(c=c),
            "src": f"src/nlc_lossy.sv:{r['line']}", "role": r["role"]}


def state_row(c: int) -> dict:
    r = build()["state_rows"]
    return {"term": f"rANS state row of channel {c}", "rtl": r["rtl"].format(c=c),
            "netlist_gate": r["netlist_gate"].format(c=c), "src": f"src/nlc_rans.sv:{r['line']}",
            "role": "22-bit rANS state of the channel (latch row)"}


def signal(name: str) -> dict:
    s = build()["signals"][name]
    return {"term": f"shared net {name}", "rtl": s["rtl"], "src": f"{s['file']}:{s['line']}",
            "role": s["role"]}


def block(key: str) -> dict:
    b = build()["blocks"][key]
    return {"term": key, "rtl": b["rtl"], "src": f"{b['file']}:{b['line']}", "role": b["role"]}


def table(n_ch: int = 8) -> str:
    m = build()
    out = [f"top {m['top_module']}, lossy core {m['lossy']}", "",
           f"per-channel latch rows (one clock gate per row per channel), c = 0..{n_ch - 1}:"]
    for f, r in m["rows"].items():
        out.append(f"  row {f:4s} {r['rtl']:36s} gate {r['netlist_gate'].split('.', 3)[3]:34s}"
                   f" src/nlc_lossy.sv:{r['line']:<4d} {r['role']}")
    s = m["state_rows"]
    out.append(f"  rANS state  {s['rtl']}   gate ...{s['netlist_gate'].split(s['inst'])[1]}   "
               f"src/nlc_rans.sv:{s['line']}")
    out += ["", "shared nets / registers:"]
    for k, s in m["signals"].items():
        out.append(f"  {k:5s} {s['rtl']:40s} {s['file']}:{s['line']}  {s['role']}")
    out += ["", "blocks:"]
    for k, b in m["blocks"].items():
        out.append(f"  {k:13s} {b['rtl']:52s} {b['file']}:{b['line']}  {b['role']}")
    return "\n".join(out)


if __name__ == "__main__":
    if "--json" in sys.argv:
        print(json.dumps(build(), indent=1))
    else:
        print(table())
