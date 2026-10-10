"""T-GL-3: sky130 cell models and LibreLane SDF ready for a real-delay gate-level simulation.

  python scripts/sdf_prep.py --pdk <sky130_fd_sc_hd dir> --netlist nl.v --sdf design.sdf --out <dir>

Writes <out>/pdk/verilog/{primitives.v,sky130_fd_sc_hd.v} (the PDK models, specify blocks kept)
and <out>/sim.sdf. In the PDK models a sized cell (`sky130_fd_sc_hd__dfrtp_1`) is a wrapper
around its base cell (`sky130_fd_sc_hd__dfrtp base (...)`), and the specify block (path delays,
timing checks) lives in the base. OpenSTA's SDF names the sized cell, so each CELL entry whose
type has no specify block of its own is retargeted to `<instance>.base` with the base cell type;
INTERCONNECT entries stay on the wrapper's ports. <out>/sdf_prep.txt: what was done.
"""

from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path

MOD = re.compile(r"^\s*module\s+(\w+)(.*?)^\s*endmodule", re.S | re.M)
BASE = re.compile(r"^\s*(sky130_fd_sc_hd__\w+)\s+base\s*\(", re.M)
CELL = re.compile(r'\(CELL\s*\(CELLTYPE\s+"(\w+)"\)\s*\(INSTANCE\s*([^)]*)\)')


def models(text: str) -> dict[str, str]:
    """module name -> body; the last definition wins (`ifdef'd variants, FUNCTIONAL off)."""
    return {m.group(1): m.group(2) for m in MOD.finditer(text)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--pdk", type=Path, required=True)
    ap.add_argument("--netlist", type=Path, required=True)
    ap.add_argument("--sdf", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()

    vdir = a.out / "pdk" / "verilog"
    vdir.mkdir(parents=True, exist_ok=True)
    for f in ("primitives.v", "sky130_fd_sc_hd.v"):
        shutil.copy(a.pdk / "verilog" / f, vdir / f)
    lib = (vdir / "sky130_fd_sc_hd.v").read_text()
    mods = models(lib)
    has_spec = {m.group(1) for m in MOD.finditer(lib)           # any `ifdef'd variant
                if re.search(r"^\s*specify\b", m.group(2), re.M)}

    used = set(re.findall(r"^\s*(sky130_fd_sc_hd__\w+)\s+\S+\s*\(", a.netlist.read_text(), re.M))
    log = [f"models: {len(mods)} modules, {len(has_spec)} with a specify block",
           f"netlist: {len(used)} cell types"]

    retarget: dict[str, str] = {}
    for t in sorted(used):
        if t not in mods:
            log.append(f"  MISSING model {t}")
            continue
        if t in has_spec:
            continue
        b = BASE.search(mods[t])
        if b and b.group(1) in has_spec:
            retarget[t] = b.group(1)
    nospec = sorted(t for t in used if t in mods and t not in has_spec and t not in retarget)
    log.append(f"specify in the sized cell: {len(used & has_spec)}; in the base (retargeted): "
               f"{len(retarget)}; none: {len(nospec)} {' '.join(nospec)}")

    n = {"kept": 0, "retargeted": 0}

    def sub(m: re.Match) -> str:
        t, inst = m.group(1), m.group(2).strip()
        if t in retarget and inst:
            n["retargeted"] += 1
            return f'(CELL (CELLTYPE "{retarget[t]}") (INSTANCE {inst}.base)'
        n["kept"] += 1
        return m.group(0)

    sdf = CELL.sub(sub, a.sdf.read_text())
    (a.out / "sim.sdf").write_text(sdf)
    log.append(f"SDF CELL entries: {n['kept']} kept, {n['retargeted']} retargeted to .base")
    for kind in ("IOPATH", "INTERCONNECT", "SETUP", "HOLD", "SETUPHOLD", "RECOVERY", "REMOVAL",
                 "WIDTH", "COND"):
        log.append(f"  ({kind}: {len(re.findall(r'[(]' + kind + r'\b', sdf))}")
    for t in ("sky130_fd_sc_hd__dfrtp", "sky130_fd_sc_hd__dlclkp"):   # what the checks look like
        if t in mods:
            body = mods[t]
            s = body.find("specify")
            log.append(f"--- {t} specify ---\n" + (body[s:s + 1500] if s >= 0 else "(none)"))
    (a.out / "sdf_prep.txt").write_text("\n".join(log) + "\n")
    print("\n".join(log))


if __name__ == "__main__":
    main()
