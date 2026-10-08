"""Run the nlc check flow (scripts/flow/flow.py) in the nlc-flow Docker image.

  python scripts/check.py                    everything (~12 min)
  python scripts/check.py --only synth,sta   selected steps: model lint rtl core synth sta gl power compress algo
  python scripts/check.py --quick            compression on 32 held-out files instead of 160
  python scripts/check.py --update-baseline  record this run as known-good
  python scripts/check.py --native ...       run on the host (tools on PATH, NLC_PDK set)

First time: python docker/fetch_inputs.py && docker build -t nlc-flow docker
Results: reports/latest/summary.md (table + details), metrics.json, all logs.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "nlc-flow"


def gds_matches_src() -> list[str]:
    """Environment for the layout step: does src/ still match the routed design's commit?
    (git runs here on the host; the image has none)"""
    latest = ROOT / "data" / "gds" / "LATEST"
    if not latest.exists():
        return []
    src = latest.parent / latest.read_text().strip() / "source.json"   # <sha7> or <sha7>-cts
    if not src.exists():
        return []
    sha = json.loads(src.read_text())["commit"]
    code = subprocess.call(["git", "diff", "--quiet", sha, "--", "src", "info.yaml"], cwd=ROOT,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return [f"NLC_GDS_MATCHES_SRC={1 if code == 0 else 0}"] if code in (0, 1) else []


def main() -> None:
    args = sys.argv[1:]
    env = gds_matches_src()
    if "--native" in args:
        args.remove("--native")
        sys.exit(subprocess.call([sys.executable, "scripts/flow/flow.py", *args], cwd=ROOT,
                                 env={**os.environ, **dict(e.split("=") for e in env)}))
    if subprocess.call(["docker", "image", "inspect", IMAGE], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL):
        sys.exit(f"docker image {IMAGE} not found: python docker/fetch_inputs.py && "
                 f"docker build -t {IMAGE} docker   (and is Docker running?)")
    sys.exit(subprocess.call(["docker", "run", "--rm", "-v", f"{ROOT}:/work", "-w", "/work",
                              *[a for e in env for a in ("-e", e)],
                              IMAGE, "python", "scripts/flow/flow.py", *args]))


if __name__ == "__main__":
    main()
