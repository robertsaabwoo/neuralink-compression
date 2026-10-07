"""Everything a new session needs, from one read-only command (no simulation, ~1 s).

  python scripts/status.py               all sections
  python scripts/status.py tests budgets  selected sections

Sections: direction repo platform tests results budgets todo commands
Sources: HANDOFF.md, docs/platform.md, docs/testing.md, docs/budgets.md,
scripts/flow/budgets.json, reports/latest/metrics.json, test*/results.xml, git.
To produce fresh results run `python scripts/check.py` (~12 min).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "flow"))
from flow import BUDGETS, fmt, score  # noqa: E402  (same scoring as check.py)

SECTIONS = ["direction", "repo", "platform", "tests", "results", "budgets", "todo", "commands"]


def head(title: str) -> None:
    print(f"\n=== {title} " + "=" * max(0, 72 - len(title)))


def md_section(path: Path, start: str, stop: str | None = None) -> str:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    i = text.find(start)
    if i < 0:
        return f"({path.relative_to(ROOT)}: section '{start}' not found)"
    j = text.find(stop, i + len(start)) if stop else -1
    return text[i:j if j > 0 else len(text)].strip()


def sh(*cmd: str) -> str:
    try:
        return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def age(path: Path) -> str:
    if not path.exists():
        return "never"
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------

def direction() -> None:
    head("direction (HANDOFF.md)")
    path = ROOT / "HANDOFF.md"            # local working notes, not in git
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    m = re.search(r"\*\*Current direction.*?(?=\n\n)", text, re.S)
    print(m.group(0) if m else "(no 'Current direction' paragraph in HANDOFF.md)")
    print("\nDetails: HANDOFF.md, docs/platform.md, docs/testing.md, docs/budgets.md")


def repo() -> None:
    head("repo")
    branch = sh("git", "rev-parse", "--abbrev-ref", "HEAD")
    base = sh("git", "log", "-1", "--format=%h %s")
    print(f"branch {branch}, HEAD {base}")
    print(f"remotes: {', '.join(sorted(set(sh('git', 'remote').split()))) or 'none'}")
    status = sh("git", "status", "--porcelain").splitlines()
    deleted = [l[3:] for l in status if l.startswith(" D")]
    print(f"working tree: {sum(l.startswith('??') for l in status)} untracked, "
          f"{sum(l[:2] == ' M' for l in status)} modified, {len(deleted)} deleted")
    if deleted:
        print("PENDING (user): template files missing from the working tree, restore with\n"
              "  git checkout -- " + " ".join(deleted))
    if not sh("git", "log", "--format=%h", "upstream/main..HEAD"):
        print("nothing committed on top of the TT template yet")
    wavs = len(list((ROOT / "data" / "raw").glob("*.wav")))
    print(f"dataset: {wavs} WAV files in data/raw" + ("" if wavs else " (python scripts/fetch_data.py)"))
    img = sh("docker", "images", "nlc-flow", "--format", "{{.Size}} {{.CreatedSince}}")
    print(f"docker image nlc-flow: {img or 'missing or Docker not running'}")
    cfg = ROOT / "src" / "config.json"
    if cfg.exists():
        print(f"TT CLOCK_PERIOD: {json.loads(cfg.read_text()).get('CLOCK_PERIOD')} ns")


def platform() -> None:
    head("platform constraints (docs/platform.md, sections 5-6)")
    p = ROOT / "docs" / "platform.md"
    print(md_section(p, "## 5.", "## Sources"))


def tests() -> None:
    head("test inventory (docs/testing.md section 2)")
    suites = [("model/tests (pytest)", sorted((ROOT / "model" / "tests").glob("test_*.py")), r"^def test_"),
              ("test/lossy (cocotb)", [ROOT / "test/lossy/test_lossy.py"], r"^@cocotb\.test"),
              ("test/rans (cocotb)", sorted((ROOT / "test" / "rans").glob("test_*.py")), r"^@cocotb\.test"),
              ("test/ TT top (cocotb)", [ROOT / "test/test_plumbing.py", ROOT / "test/test_modes.py"],
               r"^@cocotb\.test")]
    for name, files, pat in suites:
        n = sum(len(re.findall(pat, f.read_text(encoding="utf-8"), re.M)) for f in files if f.exists())
        print(f"  {name:24s} {n:3d} test functions in {len(files)} file(s)")
    print("  (pytest counts parametrised cases separately: 47)")
    print("  + check.py steps: lint, synth, sta, gl (gate level), power, compress")


# per-step results: info keys written by scripts/flow/flow.py
STEP_KEYS = [("model", "model_tests"), ("lint", "lint_warnings_top"), ("rtl", "rtl_tests_passed"),
             ("synth", "lossy_flops"), ("sta", "critical_path_ss_ns"),
             ("gl", "gl_unit_delay_ok_at_tt_clock"), ("power", "power_uw"),
             ("compress", "compression")]


def results() -> None:
    head("last results per check.py step (newest run that ran the step)")
    runs = sorted((p for p in (ROOT / "reports").glob("2*") if (p / "metrics.json").exists()),
                  reverse=True)
    loaded = [(p.name, json.loads((p / "metrics.json").read_text())) for p in runs]
    if not loaded:
        print("  check.py: never run (python scripts/check.py)")
    for step, key in STEP_KEYS:
        hit = next(((n, m) for n, m in loaded if key in m.get("info", {})), None)
        if hit is None:
            print(f"  {step:8s} never run")
            continue
        n, m = hit
        extra = [t for t in m.get("failed_tests", [])]
        print(f"  {step:8s} {n}: {key} = {m['info'][key]}")
        if step == "rtl":
            for t in m.get("pending_tests", []):
                print(f"           PENDING {t} (mode not implemented yet)")
        for t in extra:
            print(f"           FAILED  {t}")
    if loaded:
        print("  history (overall): " + ", ".join(f"{n}={m.get('overall')}" for n, m in loaded[:6]))
    print("\n  raw cocotb results.xml (whatever `make` ran last in that folder: may be a replay,")
    print("  gate-level or filtered run; the per-step lines above are authoritative):")
    for xml in [ROOT / "test/lossy/results.xml", ROOT / "test/rans/results.xml", ROOT / "test/results.xml"]:
        if not xml.exists():
            print(f"  {xml.relative_to(ROOT)}: never run")
            continue
        cases = []
        for tc in ET.parse(xml).getroot().iter("testcase"):
            bad = any(c.tag in ("failure", "error") for c in tc)
            cases.append(("FAIL " if bad else "pass ") + tc.get("name"))
        npass = sum(c.startswith("pass") for c in cases)
        print(f"  {xml.relative_to(ROOT)} ({age(xml)}): {npass}/{len(cases)} pass")
        for c in cases:
            if c.startswith("FAIL"):
                print(f"      {c}")


def budgets() -> None:
    head("metrics vs budgets (scripts/flow/budgets.json; rationale docs/budgets.md)")
    mj = ROOT / "reports" / "latest" / "metrics.json"
    vals = json.loads(mj.read_text())["metrics"] if mj.exists() else {}
    print("  values carry over from earlier runs when the latest run was partial\n")
    rows = [("status", "metric", "value", "limit", "target", "baseline")]
    for name, b in BUDGETS["metrics"].items():
        v = vals.get(name)
        st, _ = score(name, v)
        rows.append((st, name, fmt(v), f"{b['cmp']} {fmt(b['limit'])}", fmt(b["target"]),
                     fmt(BUDGETS["baseline"].get(name))))
    w = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        print("  " + "  ".join(c.ljust(w[i]) for i, c in enumerate(r)))
    info = json.loads(mj.read_text()).get("info", {}) if mj.exists() else {}
    for k in ("power_uw", "critical_path_ss_ns", "lossy_flops", "compression"):
        if k in info:
            print(f"  {k}: {info[k]}")


def todo() -> None:
    head("tests to build (docs/testing.md section 4)")
    sec = md_section(ROOT / "docs" / "testing.md", "## 4.")
    for line in sec.splitlines():
        if line.startswith("### "):
            print("  " + line[4:])
        elif re.match(r"^\d+\. ", line):
            print("    " + line)
    head("next steps (HANDOFF.md)")
    print(md_section(ROOT / "HANDOFF.md", "## Next steps"))


def commands() -> None:
    head("commands (docs/testing.md section 1)")
    print(md_section(ROOT / "docs" / "testing.md", "| command |", "`check.py` runs"))


def main() -> None:
    want = sys.argv[1:] or SECTIONS
    bad = [s for s in want if s not in SECTIONS]
    if bad:
        sys.exit(f"unknown section(s) {bad}; choose from {SECTIONS}")
    for s in want:
        globals()[s]()
    print()


if __name__ == "__main__":
    main()
