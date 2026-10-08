"""Per-pin switching activity from a gate-level VCD, as OpenSTA `set_power_activity` commands.

OpenSTA's read_vcd annotates pins, so it needs every cell's ports in the VCD; we
dump only the netlist's top-level nets (test/lossy/gl_dump.v, test/core/gl_dump.v, test/tb.v
+vcd) and map each net to the cell output pin (or input port) that drives it.

Two activities per net, both in transitions per clock cycle (OpenSTA's -activity):
  functional  value sampled at every rising clock edge: glitch-free, what a
              zero-delay simulation sees (the budgeted number)
  raw         every transition in the VCD, including the glitches produced by
              the unit-delay (#1 per cell) gate models: an upper-bound-ish view
Clock-network nets (netlist.clock_network: the port, clock buffers, clock gate outputs) always
use raw transitions: their pulses are real, and sampling at the clock edge would see them
constant (free clock power). Duty = fraction of clock edges at which the net is 1.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from netlist import read_lib, read_netlist  # noqa: E402


def parse_vcd(path: Path, clock: str = "clk"):
    """Per net bit: (functional toggles, raw toggles, edges high); and the number of edges."""
    bits: dict[str, list[tuple[str, int]]] = {}         # id -> [(bit name, index in value)]
    width: dict[str, int] = {}
    clk_id = None
    with open(path) as f:
        for line in f:
            if line.startswith("$var"):
                t = line.split()
                w, ident, name = int(t[2]), t[3], t[4]
                width[ident] = w
                if w == 1:
                    bits.setdefault(ident, []).append((name, 0))
                else:
                    lsb = int(t[5].strip("[]").split(":")[-1]) if len(t) > 6 else 0
                    for k in range(w):                   # VCD values are MSB first
                        bits.setdefault(ident, []).append((f"{name}[{lsb + w - 1 - k}]", k))
                if name == clock and w == 1:
                    clk_id = ident
            elif line.startswith("$enddefinitions"):
                break
        if clk_id is None:
            raise ValueError(f"{path}: no {clock}")
        cur = {i: "x" * w for i, w in width.items()}
        sampled = dict(cur)
        func = {i: [0] * w for i, w in width.items()}
        raw = {i: [0] * w for i, w in width.items()}
        high = {i: [0] * w for i, w in width.items()}   # edges at which the bit was 1
        since = {i: 0 for i in width}                   # edge count when `sampled` last changed
        dirty: set[str] = set()
        edges = 0

        def settle(i: str) -> None:                    # credit high time of the old sample
            n = edges - since[i]
            if n and "1" in sampled[i]:
                hi = high[i]
                for k, ch in enumerate(sampled[i]):
                    if ch == "1":
                        hi[k] += n
            since[i] = edges
        for line in f:
            if not line or line[0] in "#$\n":
                continue
            if line[0] == "b":
                val, ident = line[1:].split()
            else:
                val, ident = line[0], line[1:].strip()
            if ident not in cur:
                continue
            w = width[ident]
            val = val.rjust(w, val[0] if val[0] in "xz" else "0")[-w:]
            prev = cur[ident]
            if ident == clk_id and prev == "0" and val == "1":
                for i in dirty:                          # sample changed nets at the edge
                    s, c = sampled[i], cur[i]
                    if s == c:
                        continue
                    fi = func[i]
                    for k in range(width[i]):
                        if s[k] != c[k] and s[k] in "01" and c[k] in "01":
                            fi[k] += 1
                    settle(i)
                    sampled[i] = c
                dirty.clear()
                edges += 1
            ri = raw[ident]
            for k in range(w):
                if prev[k] != val[k] and prev[k] in "01" and val[k] in "01":
                    ri[k] += 1
            cur[ident] = val
            dirty.add(ident)
        for i in width:
            settle(i)
    out = {}
    for ident, names in bits.items():
        for name, k in names:
            out[name.lstrip("\\")] = (func[ident][k], raw[ident][k], high[ident][k])
    return out, edges


def activity_tcl(netlist: Path, vcd: Path, lib: Path, out_func: Path, out_raw: Path,
                 clock: str = "clk") -> dict:
    """Write set_power_activity scripts (functional and raw); returns coverage stats."""
    stats, edges = parse_vcd(vcd, clock)
    if edges == 0:
        raise ValueError(f"{vcd}: no rising edges on {clock}")
    nl = read_netlist(netlist, read_lib(lib), clock)
    func, raw = [], []

    def emit(target: str, s: tuple[int, int, int]) -> None:
        duty = min(1.0, max(0.0, s[2] / edges))
        func.append(f"set_power_activity {target} -activity {s[0] / edges:.6g} -duty {duty:.4g}")
        raw.append(f"set_power_activity {target} -activity {s[1] / edges:.6g} -duty {duty:.4g}")

    driven = missing = clock_pins = 0
    clock_toggles = 0
    for c in nl.cells.values():
        for pin, net in c.outs.items():
            driven += 1
            s = stats.get(net)
            if s is None:
                missing += 1
                continue
            if net in nl.clock_nets:
                s = (s[1], s[1], s[1] // 4)      # duty ~0.5 for a free-running clock
                clock_pins += 1
                clock_toggles += s[1]
            emit(f"-pins [get_pins {{{c.inst}/{pin}}}]", s)
    for b in nl.inputs:
        if b != clock and b in stats:
            emit(f"-input_ports [get_ports {{{b}}}]", stats[b])
    out_func.write_text("\n".join(func) + "\n")
    out_raw.write_text("\n".join(raw) + "\n")
    f_tot = sum(s[0] for s in stats.values())
    r_tot = sum(s[1] for s in stats.values())
    return {"clock_edges": edges, "driven_pins": driven, "unannotated_pins": missing,
            "clock_net_pins": clock_pins,
            "clock_net_toggles_per_cycle": round(clock_toggles / edges, 2),
            "glitch_factor": round(r_tot / f_tot, 2) if f_tot else None}
