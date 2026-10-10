"""Clock-gate observation map (DFT mode 2, decision D11): DBG[7:4] group, uo_out bit -> gate.

In DBG mode 2 the TT top shows on uo_out the enables of the 8 clock gates (nlc_icg, one
sky130 dlclkp each) of the group in DBG[7:4], registered every clock: bit b of uo_out = the
enable (dlclkp GATE input) of gate (group, b) at the clock edge that loaded uo_out. This
table is the single description of the wiring in the RTL (the dbg_obs terms in project.v,
nlc_core.v, nlc_cfg.v, nlc_slot_sel.v, nlc_encoder.v, nlc_lossy.sv, nlc_rans.sv);
test/test_dft.py checks the RTL against it and --netlist checks that it names every gate of
a synthesised netlist exactly once.

  python scripts/dft/icg_map.py                 markdown table, a row per group (docs/architecture.md)
  python scripts/dft/icg_map.py --json          [{group, bit, rtl, cell, parent}, ...]
  python scripts/dft/icg_map.py --netlist NL.v  completeness against a Yosys / LibreLane netlist

Columns: `rtl` is the instance whose `en` port is the gate enable (an nlc_icg, or the
nlc_greg / nlc_rreg that contains the gate); `cell` the dlclkp instance in the synthesised
netlist; `parent` the clock the gate hangs off. A gate's GCLK pulses only when its enable
AND every parent gate's enable are 1: a child's enable can read 1 while its parent is closed.
Limit: this observes the enable logic, not the gate cell or the clock tree (a dlclkp whose
CLK pin is unconnected, results.md F27, still shows its enable).
"""

from __future__ import annotations

import argparse
import json
import re
import sys

N_CH = 8
LOSSY = "core.u_enc.u_lossy"
RANS = f"{LOSSY}.u_rans"
ROWS = ["u_sx", "u_e1", "u_o1", "u_dp1", "u_e2", "u_o2", "u_dp2", "u_e3", "u_o3", "u_dp3", "u_qa"]

# how each owner kind maps to its dlclkp cell (synthesis: nlc_icg.u_cg; nlc_greg.u_icg;
# nlc_rreg with LATCH = 1 -> g_l.u_r = nlc_lreg -> u_icg)
CELL = {"icg": ".u_cg", "greg": ".u_icg.u_cg", "rreg": ".g_l.u_r.u_icg.u_cg"}


def table() -> list[dict]:
    g: list[list[tuple[str, str, str] | None]] = []
    # groups 0..10: per-channel wavelet rows of nlc_lossy, bit = channel
    for r in ROWS:
        par = f"{LOSSY}.clk_s" if r == "u_sx" else f"{LOSSY}.clk_i"
        g.append([(f"{LOSSY}.g_ch[{c}].{r}", "rreg", par) for c in range(N_CH)])
    # group 11: rANS state rows, bit = channel
    g.append([(f"{RANS}.g_st[{c}].u_st", "rreg", f"{LOSSY}.clk_l") for c in range(N_CH)])
    # group 12: nlc_lossy block gates + the coder's parent gate
    g.append([
        (f"{LOSSY}.u_cg_l", "icg", "core.clk_core"),
        (f"{LOSSY}.u_cg_s", "icg", f"{LOSSY}.clk_l"),
        (f"{LOSSY}.u_cg_i", "icg", f"{LOSSY}.clk_l"),
        (f"{LOSSY}.u_x", "greg", f"{LOSSY}.clk_i"),
        (f"{LOSSY}.g_sg.u_sg", "greg", f"{LOSSY}.clk_i"),
        (f"{LOSSY}.g_sg.u_sgx", "greg", f"{LOSSY}.clk_s"),
        (f"{LOSSY}.u_cg_o", "icg", f"{LOSSY}.clk_l"),
        (f"{RANS}.u_cg_c", "icg", f"{LOSSY}.clk_l"),
    ])
    # group 13: coder data gates, encoder, slot selector, core gate, raw-bypass gate
    g.append([
        (f"{RANS}.g_loop.u_xl", "greg", f"{LOSSY}.clk_l"),
        (f"{RANS}.u_o", "greg", f"{LOSSY}.clk_l"),
        ("core.u_enc.u_cg", "icg", "core.clk_core"),
        ("core.u_sel.u_cg_s", "icg", "core.clk_core"),
        ("core.u_sel.u_cg_h", "icg", "core.clk_core"),
        ("core.u_sel.u_smp", "greg", "core.u_sel.gclk_h"),
        ("core.u_cg_core", "icg", "clk"),
        ("core.u_cg_raw", "icg", "core.clk_core"),
    ])
    # group 14: config slot registers, bit = channel
    g.append([(f"core.u_cfg.g_slot[{c}].u_slot", "greg", "clk") for c in range(N_CH)])
    # group 15: config control gate, TT top gates; bit 7 unused (reads 0)
    g.append([
        ("core.u_cfg.u_cg", "icg", "clk"),
        ("u_cg_pins", "icg", "clk"),
        ("u_cg_frm", "icg", "clk"),
        ("u_cg_dat", "icg", "clk"),
        ("u_cfg_addr", "greg", "clk"),
        ("u_cfg_data", "greg", "clk"),
        ("u_out", "greg", "clk"),
        None,
    ])
    out = []
    for gi, grp in enumerate(g):
        assert len(grp) == 8, gi
        for b, e in enumerate(grp):
            if e is None:
                continue
            rtl, kind, par = e
            out.append(dict(group=gi, bit=b, rtl=rtl, cell=rtl + CELL[kind], parent=par))
    return out


def norm(name: str) -> str:
    """Netlist instance name -> dotted path (Yosys escapes: \\core.u_x.g_ch[3].u .. )."""
    return name.lstrip("\\").replace("\\", "")


def check_netlist(path: str) -> int:
    text = open(path).read()
    cells = [norm(m.group(1)) for m in
             re.finditer(r"sky130_fd_sc_hd__s?dlclkp_\d+\s+(\S+)\s*\(", text)]
    want = {e["cell"] for e in table()}
    have = set(cells)
    if not any("." in c for c in cells):          # our Yosys flow renames cells (_123_)
        print(f"{len(cells)} dlclkp cells in {path}, {len(want)} in the map "
              "(cell names not hierarchical: count only)")
        return 0 if len(cells) == len(want) else 1
    missing, extra = sorted(want - have), sorted(have - want)
    dup = len(cells) - len(have)
    print(f"{len(cells)} dlclkp cells in {path}, {len(want)} in the map")
    for m in missing:
        print(f"  in map, not in netlist: {m}")
    for x in extra:
        print(f"  in netlist, not in map: {x}")
    if dup:
        print(f"  {dup} duplicate cell names")
    return 1 if missing or extra or dup else 0


def markdown() -> str:
    """One row per group. bit b = the `en` of the listed instance; `[b]`: bit b = channel b;
    `-` reads 0. Paths are below the TT top (`tt_um_nlc_compressor`)."""
    t = table()
    rows = ["| group | gates (`en` of), bit 0 first | parent clock |", "|---|---|---|"]
    for g in range(16):
        es = {e["bit"]: e for e in t if e["group"] == g}
        paths = [es[b]["rtl"] if b in es else None for b in range(8)]
        pat = {p.replace(f"[{b}]", "[b]") for b, p in enumerate(paths) if p}
        if len(pat) == 1 and len(es) == 8 and "[b]" in next(iter(pat)):
            cell = f"`{pat.pop()}` (b = channel)"
        else:
            parts = [p.split(".") for p in paths if p]
            k = 0
            while all(len(q) > k + 1 and q[k] == parts[0][k] for q in parts):
                k += 1
            pre = ".".join(parts[0][:k])
            cell = (f"`{pre}.` " if pre else "") + ", ".join(
                f"{b}: `{'.'.join(p.split('.')[k:])}`" if p else f"{b}: -"
                for b, p in enumerate(paths))
        par = sorted({e["parent"] for e in es.values()})
        rows.append(f"| {g} | {cell} | {', '.join(f'`{x}`' for x in par)} |")
    return "\n".join(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--netlist")
    a = ap.parse_args()
    if a.netlist:
        sys.exit(check_netlist(a.netlist))
    print(json.dumps(table(), indent=1) if a.json else markdown())


if __name__ == "__main__":
    main()
