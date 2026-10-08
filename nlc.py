"""One command for every measurement. Thin wrapper over scripts/check.py (Docker image nlc-flow).

  python nlc.py algo       model tests + compression + algorithm evaluation (rate, SNR,
                           generalisation, spikes, max error)                   T-ALG-*, T-CHG-*
  python nlc.py sim        model, lint, RTL suites, nlc_core at the real interface   T-IF/BW/OVF/ROB/LAT
  python nlc.py synth      Yosys area, area vs N_SEL sweep                     T-AREA-1/2
  python nlc.py sta        timing (synthesises first if there is no netlist)    T-STA-1
  python nlc.py gate_sim   gate-level lossy core + TT top                       T-GL-1/2
  python nlc.py power      gate-level power scenarios + idle                    T-PWR-1
  python nlc.py layout     routed design from TT's GDS action (fetched with gh if HEAD's is not
                           in data/gds): real-data power with the clock tree, buffer trees and
                           slews per RTL signal (reports/latest/layout_fanout.md)  T-PWR-3, T-FAN-1
  python nlc.py all        everything (T2 tier, ~1 h)
  python nlc.py report     print the last report (reports/latest/summary.md)
  python nlc.py accept     accept the current bitstream as the new golden reference (T-CHG-7)

Extra flags go to the flow: --quick (short versions), --native (no Docker),
--update-baseline. Results: reports/latest/summary.md, metrics.json, per-test JSON in
reports/latest/core/. Status overview: python scripts/status.py.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STEPS = {
    "algo": "model,compress,algo",
    "sim": "model,lint,rtl,core",
    "synth": "synth,area",
    "sta": "sta",
    "gate_sim": "gl",
    "power": "power",
    "layout": "synth,layout",      # synth: the pre-layout netlist the layout numbers compare to
}
NEEDS_NETLIST = {"sta", "gate_sim", "power"}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        sys.exit(__doc__)
    cmd, extra = sys.argv[1], sys.argv[2:]
    if cmd == "report":
        summary = ROOT / "reports" / "latest" / "summary.md"
        sys.exit(print(summary.read_text()) if summary.exists() else f"{summary} missing: run a step")
    if cmd == "accept":
        sys.exit(subprocess.call([sys.executable, "scripts/accept_golden.py", *extra], cwd=ROOT))
    if cmd == "all":
        only = []
    elif cmd in STEPS:
        steps = STEPS[cmd]
        if cmd in NEEDS_NETLIST and not (ROOT / "reports/latest/lossy_netlist.v").exists():
            steps = "synth," + steps
        only = ["--only", steps]
    else:
        sys.exit(f"unknown command {cmd!r}\n\n{__doc__}")
    sys.exit(subprocess.call([sys.executable, "scripts/check.py", *only, *extra], cwd=ROOT))


if __name__ == "__main__":
    main()
