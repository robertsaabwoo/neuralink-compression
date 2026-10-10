"""Prepare one nightly entry (.github/workflows/nightly.yaml): take the design from `ref`, then
apply LibreLane config overrides and the tile count. Tooling stays from the nightly branch.

Entry fields (sweeps/nightly.json):
  name     unique id (artifact nightly-<name>)
  ref      branch or commit holding the design (src/, info.yaml, test/mock)
  flow     "full" (routed, sign-off checks of LibreLane) or "preview" (stop after post-CTS repair)
  noid     true: patched LibreLane image without CTS latency balancing (not TT-standard)
  config   {LibreLane variable: value} merged into src/config.json
  tiles    e.g. "4x2" (info.yaml)
  rtl jobs: filter (COCOTB_TEST_FILTER for test/core), seeds ("1 2 3"), env ("NLC_LONG=1")

  python scripts/ci/nightly_apply.py entry.json
"""

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    e = json.loads(Path(sys.argv[1]).read_text())
    ref = e["ref"]
    rev = f"origin/{ref}" if subprocess.run(["git", "rev-parse", "--verify", "-q", f"origin/{ref}"],
                                            cwd=ROOT, capture_output=True).returncode == 0 else ref
    subprocess.run(["git", "checkout", rev, "--", "src", "info.yaml", "test/mock"], cwd=ROOT, check=True)
    cfg = ROOT / "src" / "config.json"
    text = cfg.read_text()
    for key, value in (e.get("config") or {}).items():
        line = f'  "{key}": {json.dumps(value)},'
        pat = re.compile(rf'^\s*"{re.escape(key)}"\s*:.*?,\s*$', re.M)
        if pat.search(text):
            text = pat.sub(line, text, count=1)
        else:                                   # new variable: after CLOCK_PORT (user section)
            anchor = '  "CLOCK_PORT": "clk",\n'
            assert anchor in text, "CLOCK_PORT anchor missing in src/config.json"
            text = text.replace(anchor, anchor + line + "\n", 1)
    cfg.write_text(text)
    if e.get("tiles"):
        info = ROOT / "info.yaml"
        info.write_text(re.sub(r'tiles:\s*"[^"]+"', f'tiles: "{e["tiles"]}"', info.read_text(), 1))
    print(f"entry {e['name']}: design from {rev}")
    print(subprocess.run(["git", "diff", "--stat", "HEAD", "--", "src", "info.yaml"], cwd=ROOT,
                         capture_output=True, text=True).stdout)
    print(subprocess.run(["git", "diff", "HEAD", "--", "src/config.json", "info.yaml"], cwd=ROOT,
                         capture_output=True, text=True).stdout)


if __name__ == "__main__":
    main()
