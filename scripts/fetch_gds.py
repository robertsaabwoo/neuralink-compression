"""Fetch the routed design from TT's GDS action (GitHub `gds` workflow) for the layout step.

  python scripts/fetch_gds.py              latest successful gds run of HEAD (or newest run
                                           whose `gds` job passed if HEAD has none)
  python scripts/fetch_gds.py --run <id>   a given workflow run
  python scripts/fetch_gds.py --list       recent gds runs
  python scripts/fetch_gds.py --preview [--run <id>]   a `cts_preview` workflow run instead
                                           (stops after clock-tree synthesis, ~10 min)
  python scripts/fetch_gds.py --pack runs/wokwi        CI: pack a local LibreLane run directory

Downloads the GDS_logs artifact (~1.6 GB unpacked) to a temporary directory and keeps what
`scripts/flow/flow.py --only layout` needs (docs/testing.md T-PWR-3, T-FAN-1):

  data/gds/<sha7>/nl.v           routed netlist, no power pins (clock tree, repair buffers)
  data/gds/<sha7>/{nom,min,max}.spef   extracted wire parasitics
  data/gds/<sha7>/signoff.sdc    LibreLane's sign-off constraints (propagated clock)
  data/gds/<sha7>/metrics.json   LibreLane's final metrics (area by cell class, slack, ...)
  data/gds/<sha7>/checks_<corner>.rpt   LibreLane's post-route max slew/cap/fanout checks
  data/gds/<sha7>/cts.rpt, cts.log   OpenROAD clock-tree synthesis summary and log
  data/gds/<sha7>/source.json    commit, run id, URL, kind ("routed" or "cts-preview")
  data/gds/LATEST                directory name of the last fetch

A cts-preview (data/gds/<sha7>-cts/) has no routing: the netlist and placement database
(design.odb) after OpenROAD.ResizerTimingPostCTS, no SPEF (the layout step estimates wire
parasitics from placement with OpenROAD), metrics as of that step.

Runs on the host (needs `gh`, logged in); data/ is git-ignored.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "gds"
CORNERS = ("nom_tt_025C_1v80", "nom_ss_100C_1v60", "nom_ff_n40C_1v95")


def gh(*args: str) -> str:
    return subprocess.run(["gh", *args], cwd=ROOT, check=True, capture_output=True,
                          text=True).stdout


def repo() -> str:
    """owner/name of `origin` (gh's default would be the template fork's upstream)."""
    url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=ROOT, check=True,
                         capture_output=True, text=True).stdout.strip()
    return url.removesuffix(".git").split("github.com")[-1].lstrip("/:")   # https or ssh form


def runs_of(rep: str, workflow: str, limit: int = 20) -> list[dict]:
    return json.loads(gh("run", "list", "-R", rep, "-w", workflow, "-L", str(limit), "--json",
                         "databaseId,headSha,headBranch,conclusion,status,displayTitle,createdAt"))


def job_ok(rep: str, run_id: int, job: str) -> bool:
    """The gds workflow fails when only the Pages `viewer` job fails; the `gds` job counts."""
    jobs = json.loads(gh("run", "view", str(run_id), "-R", rep, "--json", "jobs"))["jobs"]
    return any(j["name"] == job and j["conclusion"] == "success" for j in jobs)


def _copy_cts(base: Path, dest: Path) -> None:
    cts = next(base.glob("*-openroad-cts"), None)
    if cts is not None:
        for f, name in (("cts.rpt", "cts.rpt"), ("openroad-cts.log", "cts.log")):
            if (cts / f).exists():
                shutil.copy(cts / f, dest / name)


def routed(base: Path) -> bool:
    """A complete flow has extracted parasitics; a run stopped with --to still writes final/
    (views of its last step) but no SPEF."""
    return (base / "final" / "spef" / "nom").is_dir()


def pack(base: Path, dest: Path, meta: dict) -> dict:
    """Keep what the layout step needs from a LibreLane run directory (runs/wokwi)."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    final = base / "final"
    if routed(base):                                  # complete flow: routed and extracted
        top = next((final / "nl").glob("*.nl.v")).name.removesuffix(".nl.v")
        shutil.copy(final / "nl" / f"{top}.nl.v", dest / "nl.v")
        for c in ("nom", "min", "max"):
            shutil.copy(final / "spef" / c / f"{top}.{c}.spef", dest / f"{c}.spef")
        shutil.copy(next((final / "sdc").glob("*.sdc")), dest / "signoff.sdc")
        shutil.copy(final / "metrics.json", dest / "metrics.json")
        sta = next(base.glob("*-openroad-stapostpnr"), None)
        for c in CORNERS if sta is not None else ():
            if (sta / c / "checks.rpt").exists():
                shutil.copy(sta / c / "checks.rpt", dest / f"checks_{c}.rpt")
        kind = "routed"
    else:                                             # stopped early (--to): last step's views
        last = max((p for p in base.iterdir() if (p / "state_out.json").exists()),
                   key=lambda p: int(p.name.split("-")[0]))
        state = json.loads((last / "state_out.json").read_text())
        top = Path(state["nl"]).name.removesuffix(".nl.v")
        shutil.copy(last / Path(state["nl"]).name, dest / "nl.v")
        shutil.copy(last / Path(state["odb"]).name, dest / "design.odb")
        shutil.copy(last / Path(state["sdc"]).name, dest / "signoff.sdc")
        (dest / "metrics.json").write_text(json.dumps(state.get("metrics", {}), indent=2))
        kind = "cts-preview"
        meta = {**meta, "last_step": last.name}
    _copy_cts(base, dest)
    src = {**meta, "top": top, "kind": kind}
    (dest / "source.json").write_text(json.dumps(src, indent=2))
    return src


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--run", type=int, help="workflow run id")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--preview", action="store_true", help="the cts_preview workflow")
    ap.add_argument("--pack", type=Path, help="CI: LibreLane run directory to pack")
    args = ap.parse_args()

    if args.pack:                                     # in GitHub Actions, no gh needed
        sha = os.environ.get("GITHUB_SHA") or subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
        server, rep, rid = (os.environ.get(k, "") for k in
                            ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
        meta = {"commit": sha, "run_id": int(rid) if rid else None,
                "branch": os.environ.get("GITHUB_REF_NAME"),
                "url": f"{server}/{rep}/actions/runs/{rid}" if rid else None}
        dest = OUT / (sha[:7] + ("" if routed(args.pack) else "-cts"))
        src = pack(args.pack, dest, meta)
        (OUT / "LATEST").write_text(dest.name + "\n")
        print(f"packed {args.pack} ({src['kind']}) -> {dest}")
        return

    rep = repo()
    workflow, job = ("cts_preview", "preview") if args.preview else ("gds", "gds")
    runs = runs_of(rep, workflow)
    if args.list:
        for r in runs:
            print(f"{r['databaseId']}  {r['headSha'][:7]}  {r['headBranch'][:12]:12s} "
                  f"{r['status']:10s} {r['conclusion'] or '-':8s} {r['createdAt']}  "
                  f"{r['displayTitle'][:50]}")
        return
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                          text=True).stdout.strip()
    if args.run:
        run = next((r for r in runs if r["databaseId"] == args.run), None)
        if run is None:
            sys.exit(f"run {args.run} is not among the last {len(runs)} {workflow} runs (--list)")
    else:
        done = [r for r in runs if r["status"] == "completed"]
        mine = [r for r in done if r["headSha"] == head and job_ok(rep, r["databaseId"], job)]
        run = mine[0] if mine else next(
            (r for r in done if job_ok(rep, r["databaseId"], job)), None)
        if run is None:
            sys.exit(f"no {workflow} run with a passing `{job}` job (--list)")
        if run["headSha"] != head:
            print(f"note: HEAD {head[:7]} has no finished {workflow} run; "
                  f"using {run['headSha'][:7]}")
    sha = run["headSha"]
    meta = {"commit": sha, "run_id": run["databaseId"], "title": run["displayTitle"],
            "branch": run["headBranch"], "created": run["createdAt"],
            "url": f"https://github.com/{rep}/actions/runs/{run['databaseId']}"}
    with tempfile.TemporaryDirectory(prefix="nlc_gds_") as tmp:
        if args.preview:                  # already packed in CI: data/gds/<sha7>-cts/
            gh("run", "download", str(run["databaseId"]), "-R", rep, "-n", "cts_preview",
               "-D", tmp)
            packed = next(p.parent for p in Path(tmp).rglob("source.json"))
            dest = OUT / packed.name
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(packed, dest)
            src = json.loads((dest / "source.json").read_text())
            (dest / "source.json").write_text(json.dumps({**src, **meta}, indent=2))
        else:
            dest = OUT / sha[:7]
            print(f"run {run['databaseId']} ({sha[:7]}: {run['displayTitle'][:60]}) -> {dest}")
            gh("run", "download", str(run["databaseId"]), "-R", rep, "-n", "GDS_logs", "-D", tmp)
            pack(next(Path(tmp).rglob("final")).parent, dest, meta)
    (OUT / "LATEST").write_text(dest.name + "\n")
    size = sum(f.stat().st_size for f in dest.iterdir()) / 1e6
    print(f"kept {size:.0f} MB in {dest.relative_to(ROOT)}; next: "
          f"python scripts/check.py --only layout")


if __name__ == "__main__":
    main()
