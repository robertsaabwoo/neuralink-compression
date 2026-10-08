"""Structure of a flat sky130 gate-level netlist: clock network, buffer trees, cell classes.

Used by flow.py step layout (docs/testing.md T-PWR-3, T-FAN-1) on the routed netlist from TT's
GDS action, and on our own Yosys netlists:

  clock network   nets reachable from the clock port through buffers, inverters and clock
                  gates (dlclkp CLK -> GCLK): the clock tree CTS built plus our gated branches
  buffer trees    the placer/resizer splits a high-fanout net into a tree of buffers; walking
                  back from every leaf to the first non-buffer driver gives the RTL signal the
                  tree serves (`core.smp_ch[0]`), its true fanout (leaves), the buffers spent on
                  it and how deep it is
  cell classes    clock buffer, clock dummy load (CTS capacitance balancing, drives nothing),
                  clock gate, flop, repair buffer (data-net buffer), hold buffer (dlygate),
                  logic; physical cells (fill/tap/decap/antenna diode) are skipped
  clock roots     the port and every clock gate output: the buffers CTS built below each one
                  (down to the next gate), the flops it clocks, CTS latency delay buffers
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

CELL_RE = re.compile(r"^\s*(sky130_fd_sc_hd__\w+)\s+(\S+)\s*\((.*?)\);", re.S | re.M)
CONN_RE = re.compile(r"\.(\w+)\(([^()]*)\)")
ASSIGN_RE = re.compile(r"^\s*assign\s+(\S+(?:\s*\[\d+\])?)\s*=\s*(\S+?)\s*;", re.M)
PORT_RE = re.compile(r"^\s*(input|output)\s+(?:wire\s+)?(?:\[(\d+):(\d+)\]\s*)?(\w+)\s*;", re.M)
LIB_CELL_RE = re.compile(r'^\s*cell\s*\(\s*"?(\w+)"?\s*\)', re.M)
LIB_OUT_RE = re.compile(r'pin\s*\(\s*"?(\w+)"?\s*\)\s*\{[^{}]*?direction\s*:\s*"?output"?')
LIB_AREA_RE = re.compile(r"^\s*area\s*:\s*([\d.]+)", re.M)

# non-inverting buffers the flow inserts (resizer fanout/slew repair, CTS, hold fixing)
BUF_RE = re.compile(r"__(buf|bufbuf|clkbuf|clkdlybuf4s\d+|dlygate4sd\d|dlymetal6s\d+s|probec_p"
                    r"|probe_p)_\d+$")
INV_RE = re.compile(r"__(inv|clkinv|clkinvlp)_\d+$")
HOLD_RE = re.compile(r"__dlygate4sd\d_\d+$")
ICG_RE = re.compile(r"__(dlclkp|sdlclkp)_\d+$")
FLOP_RE = re.compile(r"__(e?df\w*|dl\w*tp\w*|dlx\w*|dfbbn\w*|sdf\w*|edfxbp\w*)_\d+$")
PHYS_RE = re.compile(r"__(fill|decap|tapvpwrvgnd|diode|fakediode)_\d+$")


def norm(net: str) -> str:
    """Net name as it appears in a VCD / report: no escape backslash, no spaces."""
    return net.strip().lstrip("\\").strip()


@dataclass
class Lib:
    outputs: dict[str, set[str]]
    area: dict[str, float]


def read_lib(path: Path) -> Lib:
    text = path.read_text()
    starts = [(m.start(), m.group(1)) for m in LIB_CELL_RE.finditer(text)] + [(len(text), "")]
    outs, area = {}, {}
    for (a, name), (b, _) in zip(starts, starts[1:]):
        body = text[a:b]
        outs[name] = set(LIB_OUT_RE.findall(body))
        m = LIB_AREA_RE.search(body)
        area[name] = float(m.group(1)) if m else 0.0
    return Lib(outs, area)


@dataclass
class Cell:
    type: str
    inst: str
    ins: dict[str, str]       # pin -> net
    outs: dict[str, str]


@dataclass
class Netlist:
    cells: dict[str, Cell]
    driver: dict[str, tuple[str, str]]               # net -> (inst, pin); ("", port) for inputs
    sinks: dict[str, list[tuple[str, str]]]          # net -> [(inst, pin)]; ("", port) outputs
    inputs: list[str]
    outputs: list[str]
    clock_nets: set[str] = field(default_factory=set)


def read_netlist(path: Path, lib: Lib, clock: str = "clk") -> Netlist:
    text = path.read_text()
    cells, driver, sinks = {}, {}, defaultdict(list)
    ports = {"input": [], "output": []}
    for d, hi, lo, name in PORT_RE.findall(text):
        ports[d] += [name] if not hi else [f"{name}[{i}]" for i in range(int(lo), int(hi) + 1)]
    for p in ports["input"]:
        driver[p] = ("", p)
    for typ, inst, conns in CELL_RE.findall(text):
        if PHYS_RE.search(typ):            # fill, tap, decap, antenna diodes: no logic
            continue
        c = Cell(typ, inst.lstrip("\\"), {}, {})
        for pin, net in CONN_RE.findall(conns):
            net = norm(net)
            if not net or "'" in net:              # unconnected or a constant
                continue
            if pin in lib.outputs.get(typ, ()):
                c.outs[pin] = net
                driver[net] = (c.inst, pin)
            else:
                c.ins[pin] = net
                sinks[net].append((c.inst, pin))
        cells[c.inst] = c
    # `assign port = net;` (LibreLane) and `assign a = b;`: the port is a sink of the net
    alias = {}
    for lhs, rhs in ASSIGN_RE.findall(text):
        lhs, rhs = norm(lhs.replace(" ", "")), norm(rhs)
        if "'" not in rhs:
            alias[lhs] = rhs
    for p in ports["output"]:
        sinks[alias.get(p, p)].append(("", p))
    nl = Netlist(cells, driver, dict(sinks), ports["input"], ports["output"])
    nl.clock_nets = clock_network(nl, clock)
    return nl


def clock_network(nl: Netlist, clock: str) -> set[str]:
    """Nets carrying the clock: from the port through buffers, inverters and clock gates."""
    seen, todo = {clock}, [clock]
    while todo:
        net = todo.pop()
        for inst, pin in nl.sinks.get(net, ()):
            if not inst:
                continue
            c = nl.cells[inst]
            through = ((BUF_RE.search(c.type) or INV_RE.search(c.type)) and pin == "A") or \
                      (ICG_RE.search(c.type) and pin == "CLK")
            if through:
                for out in c.outs.values():
                    if out not in seen:
                        seen.add(out)
                        todo.append(out)
    return seen


def cell_class(nl: Netlist, c: Cell) -> str:
    t = c.type
    if PHYS_RE.search(t):
        return "physical"
    if ICG_RE.search(t):
        return "clock_gate"
    if FLOP_RE.search(t):
        return "flop"
    on_clock = any(n in nl.clock_nets for n in c.outs.values())
    if (BUF_RE.search(t) or INV_RE.search(t)) and on_clock:
        return "clock_buffer"
    if (BUF_RE.search(t) or INV_RE.search(t)) and c.ins.get("A") in nl.clock_nets and \
            not any(nl.sinks.get(n) for n in c.outs.values()):
        return "clock_dummy_load"        # CTS load balancing: a clock buffer driving nothing
    if HOLD_RE.search(t):
        return "hold_buffer"
    if BUF_RE.search(t):
        return "repair_buffer"
    return "logic"


def is_tree_buffer(nl: Netlist, c: Cell) -> bool:
    """A cell that only re-drives its input net: buffers, and the delay cells used as such."""
    return bool(BUF_RE.search(c.type)) and not any(n in nl.clock_nets for n in c.outs.values())


@dataclass
class Tree:
    root: str                 # net driven by a real driver (or an input port)
    driver: str               # "inst (cell)" or "port"
    leaves: list[tuple[str, str]]   # (inst, pin) of non-buffer sinks, ("", port) for outputs
    buffers: list[str]        # instances
    nets: list[str]           # root + every buffered net
    depth: int                # buffers on the longest root -> leaf path


def trees(nl: Netlist) -> dict[str, Tree]:
    """Every data net with a real driver, expanded through the buffers below it."""
    out = {}
    for net, (inst, pin) in nl.driver.items():
        if net in nl.clock_nets:
            continue
        if inst and is_tree_buffer(nl, nl.cells[inst]):
            continue                                 # interior of some tree
        drv = f"{inst} ({nl.cells[inst].type.removeprefix('sky130_fd_sc_hd__')})" if inst \
            else "input port"
        t = Tree(net, drv, [], [], [net], 0)
        stack = [(net, 0)]
        while stack:
            n, d = stack.pop()
            t.depth = max(t.depth, d)
            for si, sp in nl.sinks.get(n, ()):
                c = nl.cells.get(si)
                if c is not None and is_tree_buffer(nl, c):
                    t.buffers.append(si)
                    for o in c.outs.values():
                        t.nets.append(o)
                        stack.append((o, d + 1))
                else:
                    t.leaves.append((si, sp))
        out[net] = t
    return out


def named_source(nl: Netlist, net: str, max_depth: int = 4) -> str:
    """Closest RTL-named nets upstream of an anonymous net (`_01234_`), for reports."""
    if not re.fullmatch(r"_\d+_|net\d+|\w*fanout\w*", net):
        return net
    seen, frontier, names = {net}, [net], []
    for _ in range(max_depth):
        nxt = []
        for n in frontier:
            inst, _ = nl.driver.get(n, ("", ""))
            if not inst:
                continue
            for i in nl.cells[inst].ins.values():
                if i in seen or i in nl.clock_nets:
                    continue
                seen.add(i)
                if re.fullmatch(r"_\d+_|net\d+", i):
                    nxt.append(i)
                else:
                    names.append(i)
        if names:
            break
        frontier = nxt
    return f"{net} <- {', '.join(sorted(set(names))[:4])}" if names else net


def clock_roots(nl: Netlist, lib: Lib, clock: str = "clk") -> list[dict]:
    """One entry per clock root (the port, each gate output): the clock buffers CTS put below
    it down to the next gate, and what they clock. `buffers` lists instances (for power)."""
    gates = [c for c in nl.cells.values() if cell_class(nl, c) == "clock_gate"]
    roots = [(clock, "input port", "")] + [
        (g.outs["GCLK"], g.inst, named_source(nl, g.ins.get("GATE", "")))
        for g in gates if "GCLK" in g.outs]
    out = []
    for net, driver, enable in roots:
        bufs, depth, flops, child, dummies = [], 0, 0, 0, 0
        stack = [(net, 0)]
        while stack:
            n, d = stack.pop()
            depth = max(depth, d)
            for inst, pin in nl.sinks.get(n, ()):
                c = nl.cells.get(inst)
                if c is None:
                    continue
                k = cell_class(nl, c)
                if k == "clock_buffer":
                    bufs.append(c.inst)
                    stack += [(o, d + 1) for o in c.outs.values()]
                elif k == "flop":
                    flops += 1
                elif k == "clock_gate":
                    child += 1
                elif k == "clock_dummy_load":
                    dummies += 1
        out.append({"root": net, "driver": driver, "enable": enable, "flops": flops,
                    "child_gates": child, "buffers": bufs, "depth": depth,
                    "delay_buffers": sum(b.startswith("delaybuf") for b in bufs),
                    "dummy_loads": dummies,
                    "buffer_area_um2": round(sum(lib.area.get(nl.cells[b].type, 0) for b in bufs)),
                    "buffer_types": _count(nl.cells[b].type for b in bufs)})
    return out


def clock_tree_summary(nl: Netlist, lib: Lib, roots: list[dict] | None = None) -> dict:
    """Clock tree as built: buffers, gates, sinks; free-running part (the port's root, before
    any gate), CTS latency delay buffers and dummy loads."""
    roots = roots if roots is not None else clock_roots(nl, lib)
    cls = {c.inst: cell_class(nl, c) for c in nl.cells.values()}
    bufs = [nl.cells[i] for i, k in cls.items() if k == "clock_buffer"]
    dummies = [nl.cells[i] for i, k in cls.items() if k == "clock_dummy_load"]
    free = roots[0]
    gated = roots[1:]
    per_gate = sorted(r["flops"] for r in gated)
    return {"clock_buffers": len(bufs),
            "clock_buffer_area_um2": round(sum(lib.area.get(c.type, 0) for c in bufs)),
            "buffer_types": dict(sorted(_count(c.type for c in bufs).items(), key=lambda x: -x[1])),
            "latency_delay_buffers": sum(c.inst.startswith("delaybuf") for c in bufs),
            "dummy_loads": len(dummies),
            "dummy_load_area_um2": round(sum(lib.area.get(c.type, 0) for c in dummies)),
            "free_running": {k: free[k] for k in ("flops", "child_gates", "depth",
                                                  "delay_buffers", "buffer_area_um2")}
            | {"buffers": len(free["buffers"])},
            "clock_gates": len(gated),
            "clock_gates_nested": len(gated) - free["child_gates"],
            "buffers_below_gates": sum(len(r["buffers"]) for r in gated),
            "flops_per_gate_median": per_gate[len(per_gate) // 2] if per_gate else None,
            "flop_clock_pins": sum(r["flops"] for r in roots),
            "clock_nets": len(nl.clock_nets)}


def _count(it) -> dict[str, int]:
    d: dict[str, int] = defaultdict(int)
    for x in it:
        d[x.removeprefix("sky130_fd_sc_hd__")] += 1
    return dict(d)


def class_area(nl: Netlist, lib: Lib) -> dict[str, float]:
    a: dict[str, float] = defaultdict(float)
    for c in nl.cells.values():
        a[cell_class(nl, c)] += lib.area.get(c.type, 0.0)
    return {k: round(v) for k, v in sorted(a.items())}
