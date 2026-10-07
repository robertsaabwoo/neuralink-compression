"""Accept the current lossy bitstream as the new reference (T-CHG-7).

  python nlc.py accept        (or: python scripts/accept_golden.py)

Run this only after an intended format change (algorithm, tables, header). It rewrites
model/tests/golden/lossy_bitstream.json and prints what changed; record the change in
docs/testing.md section 3.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "model"), str(ROOT / "model" / "tests")]

from test_change_guard import GOLDEN, golden_fingerprint  # noqa: E402


def main() -> None:
    new = json.loads(json.dumps(golden_fingerprint()))
    old = json.loads(GOLDEN.read_text()) if GOLDEN.exists() else {}
    changed = [k for k in new if new[k] != old.get(k)]
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(json.dumps(new, indent=2) + "\n")
    if not old:
        print(f"created {GOLDEN.relative_to(ROOT)}")
    elif changed:
        print(f"accepted bitstream change in: {', '.join(changed)}")
        for k in changed:
            if isinstance(new[k], dict) and "packet_bytes" in new[k]:
                print(f"  {k}: packet bytes {old.get(k, {}).get('packet_bytes')} -> "
                      f"{new[k]['packet_bytes']}")
        print("record it in docs/testing.md section 3 (results log)")
    else:
        print("no change: golden file already matches the model")


if __name__ == "__main__":
    main()
