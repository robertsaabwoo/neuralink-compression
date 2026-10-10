"""Unpack the Neuralink compression-challenge dataset into data/raw.

  python scripts/fetch_data.py                 # download the archived copy
  python scripts/fetch_data.py path/to/data.zip   # use a local copy

The original URL (https://content.neuralink.com/compression-challenge/data.zip) returns 404
since 2026-10-06. URL points to the Internet Archive snapshot of it (65 MB zip, 743 WAV files,
143 MB unpacked; checked 2026-10-10: file count and a sample hash match a local copy).
"""

from __future__ import annotations

import sys
import urllib.request
import zipfile
from pathlib import Path

URL = ("https://web.archive.org/web/20250804221732id_/"
       "https://content.neuralink.com/compression-challenge/data.zip")
ROOT = Path(__file__).resolve().parents[1] / "data"


def main() -> None:
    src = sys.argv[1] if len(sys.argv) > 1 else URL
    ROOT.mkdir(exist_ok=True)
    zpath = Path(src) if Path(src).is_file() else ROOT / "data.zip"
    url = src
    if not zpath.exists():
        print(f"downloading {url} -> {zpath}")
        urllib.request.urlretrieve(url, zpath)
    out = ROOT / "raw"
    out.mkdir(exist_ok=True)
    with zipfile.ZipFile(zpath) as z:
        for member in z.infolist():
            if member.filename.endswith(".wav"):
                (out / Path(member.filename).name).write_bytes(z.read(member))
    print(f"{len(list(out.glob('*.wav')))} WAV files in {out}")


if __name__ == "__main__":
    main()
