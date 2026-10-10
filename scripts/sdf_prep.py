"""T-GL-3: sky130 cell models and LibreLane SDF ready for a real-delay gate-level simulation.

  python scripts/sdf_prep.py --pdk <sky130_fd_sc_hd dir> --netlist nl.v --sdf design.sdf \
      --out <dir> --sim icarus|cvc

Writes <out>/pdk/verilog/primitives.v (the PDK's UDPs), <out>/pdk/verilog/sky130_fd_sc_hd.v and
<out>/sim.sdf (the SDF unchanged), and <out>/sdf_prep.txt (what was done).

The cell library is pruned to the cell types the netlist uses, each in its timing variant (open_pdks'
merged file has four per cell: power pins or not x FUNCTIONAL or not; we keep "no power pins,
not FUNCTIONAL": the specify block with path delays and $setuphold/$recrem/$width checks that
toggle a notifier into the flop UDP). Unused cells would only be elaborated as top levels.

--sim cvc: OSS CVC ignores the optional $setuphold/$recrem arguments (timestamp/timecheck
conditions, delayed reference/data nets), which leaves the cells' *_delayed nets undriven (all
flops X). Rewritten in 1364-1995 form: the conditions move onto the events (`&&& cond`) and
`assign X_delayed = X;`; $recrem is split into $recovery + $removal (CVC annotates SDF
RECOVERY/REMOVAL only onto those). The checks then work on the undelayed signals, so a negative SDF
limit (sky130 has negative hold/setup on some arcs) is clamped to 0: pessimistic, never misses
a violation the delayed-signal form would flag.
"""

from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path

MOD = re.compile(r"^\s*module\s+(\w+)\b.*?^\s*endmodule\b", re.S | re.M)
INST = re.compile(r"^\s*(sky130_fd_sc_hd__\w+)\s+(?:#\s*\(.*?\)\s*)?\\?\S+\s*\(", re.M)
INST_NAME = re.compile(r"^\s*(sky130_fd_sc_hd__\w+)\s+(\\?\S+)\s*\(", re.M)
ESC_V = re.compile(r"\\(\S+)\s")                     # Verilog escaped identifier
SDF_ID = re.compile(r"(?:[\w\[\]]|\\.)*\\.(?:[\w\[\]]|\\.)*")   # SDF name with an escape
CHECK = re.compile(r"(\$(?:setuphold|recrem))\s*\((.*?)\)\s*;", re.S)


def plain(name: str) -> str:
    """A unique plain identifier for an escaped one: non-word characters as _xx_ (hex)."""
    return "esc_" + re.sub(r"\W", lambda c: f"_{ord(c.group(0)):02x}_", name)


def split_args(s: str) -> list[str]:
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
            continue
        depth += ch == "("
        depth -= ch == ")"
        cur += ch
    out.append(cur.strip())
    return out


def cvc_form(mod: str) -> tuple[str, int]:
    """1364-1995 timing checks + driven *_delayed nets (see module doc)."""
    n = 0

    def sub(m: re.Match) -> str:
        nonlocal n
        task, args = m.group(1), split_args(m.group(2))
        if len(args) <= 5:
            return m.group(0)
        ref, data, lim1, lim2, notifier = args[:5]
        conds = (args[5:7] + ["", ""])[:2]
        if conds[0] and "&&&" not in ref:
            ref = f"{ref} &&& {conds[0]}"
        if conds[1] and "&&&" not in data:
            data = f"{data} &&& {conds[1]}"
        n += 1
        if task == "$recrem":            # CVC matches SDF RECOVERY/REMOVAL only to these
            return (f"$recovery ( {ref} , {data} , ({lim1}) , {notifier} ) ;\n"
                    f"$removal ( {ref} , {data} , ({lim2}) , {notifier} ) ;")
        return f"{task} ( {ref} , {data} , ({lim1}) , ({lim2}) , {notifier} ) ;"

    mod = CHECK.sub(sub, mod)
    delayed = re.findall(r"^\s*wire\s+(\w+)_delayed\s*;", mod, re.M)
    if delayed:
        assigns = "".join(f"    assign {d}_delayed = {d};\n" for d in delayed)
        mod = re.sub(r"^(\s*specify\b)", assigns + r"\1", mod, count=1, flags=re.M)
    return mod, n


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--pdk", type=Path, required=True)
    ap.add_argument("--netlist", type=Path, required=True)
    ap.add_argument("--sdf", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--sim", choices=("icarus", "cvc"), default="icarus")
    a = ap.parse_args()

    vdir = a.out / "pdk" / "verilog"
    vdir.mkdir(parents=True, exist_ok=True)
    shutil.copy(a.pdk / "verilog" / "primitives.v", vdir / "primitives.v")
    lib = (a.pdk / "verilog" / "sky130_fd_sc_hd.v").read_text()
    mods: dict[str, str] = {}
    for m in MOD.finditer(lib):            # the last variant of each cell: no power pins, timing
        mods[m.group(1)] = m.group(0)

    todo = set(INST.findall(a.netlist.read_text()))
    log = [f"library: {len(mods)} modules; netlist: {len(todo)} cell types"]
    keep: dict[str, str] = {}
    while todo:                            # plus any library module a kept cell instantiates
        t = todo.pop()
        if t in keep:
            continue
        if t not in mods:
            log.append(f"  MISSING model {t}")
            continue
        keep[t] = mods[t]
        todo |= {d for d in INST.findall(mods[t].split("\n", 1)[1]) if d in mods} - keep.keys()

    nospec = sorted(t for t, b in keep.items() if not re.search(r"^\s*specify\b", b, re.M))
    log.append(f"kept {len(keep)} cells, {len(keep) - len(nospec)} with a specify block; "
               f"without: {' '.join(nospec)}")
    nchk = 0
    out = ["// pruned from open_pdks sky130_fd_sc_hd.v by scripts/sdf_prep.py "
           f"(--sim {a.sim})", "`timescale 1ns / 1ps", "`default_nettype wire"]
    for t in sorted(keep):
        body = keep[t]
        if a.sim == "cvc":
            body, n = cvc_form(body)
            nchk += n
        out += ["`celldefine", body, "`endcelldefine", ""]
    (vdir / "sky130_fd_sc_hd.v").write_text("\n".join(out))
    if a.sim == "cvc":
        log.append(f"cvc: {nchk} $setuphold/$recrem rewritten to the 1364-1995 form "
                   "($recrem -> $recovery + $removal), *_delayed nets assigned")

    # INTERCONNECT into cells without a specify block (antenna diodes: no output, no function)
    # is dropped: Icarus 13 aborts on it ("Could not insert intermodpath")
    inst_type = {i.lstrip("\\"): t for t, i in INST_NAME.findall(a.netlist.read_text())}
    sdf_in = a.sdf.read_text()
    dropped = 0

    def ic(m: re.Match) -> str:
        nonlocal dropped
        dst = m.group(1).rsplit(".", 1)[0].replace("\\", "")
        if inst_type.get(dst) in nospec:
            dropped += 1
            return ""
        return m.group(0)

    sdf = re.sub(r"^\s*\(INTERCONNECT\s+\S+\s+(\S+)\s.*\n", ic, sdf_in, flags=re.M)

    # escaped identifiers (`\u_cfg_addr.u_icg.u_cg ` in the netlist, `u_cfg_addr\.u_icg\.u_cg`
    # in the SDF) become plain ones in both: Icarus 13 cannot look them up from the SDF
    # (NULL handle, abort)
    nl, n_nl = ESC_V.subn(lambda m: plain(m.group(1)) + " ", a.netlist.read_text())
    (a.out / "sim_nl.v").write_text(nl)

    def sdf_name(m: re.Match) -> str:
        parts = re.split(r"(?<!\\)\.", m.group(0))
        return ".".join(plain(p.replace("\\", "")) if "\\" in p else p for p in parts)

    sdf, n_sdf = SDF_ID.subn(sdf_name, sdf)
    log.append(f"escaped identifiers made plain: {n_nl} in the netlist (sim_nl.v), "
               f"{n_sdf} SDF names")
    (a.out / "sim.sdf").write_text(sdf)
    log.append(f"SDF: {dropped} INTERCONNECT entries into cells without a specify block dropped")
    for kind in ("CELL", "IOPATH", "INTERCONNECT", "SETUP", "HOLD", "SETUPHOLD", "RECOVERY",
                 "REMOVAL", "WIDTH", "COND"):
        pat = r"[(]" + kind + r"\b"
        log.append(f"  SDF ({kind}: {len(re.findall(pat, sdf))}")
    neg = re.findall(r"\((?:SETUP|HOLD|RECOVERY|REMOVAL)\b[^\n]*?\(-[0-9.]+", sdf)
    log.append(f"  SDF timing checks with a negative limit: {len(neg)}")
    (a.out / "sdf_prep.txt").write_text("\n".join(log) + "\n")
    print("\n".join(log))


if __name__ == "__main__":
    main()
