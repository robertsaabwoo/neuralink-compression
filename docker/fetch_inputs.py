"""Fetch the nlc-flow image's build inputs on the host (wheels + sky130_fd_sc_hd).

  python docker/fetch_inputs.py && docker build -t nlc-flow docker
"""
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PDK_URL = ("https://github.com/fossi-foundation/ciel-releases/releases/download/"
           "sky130-ff08c23db8359afce3f134c454e7930586d0641c/sky130_fd_sc_hd.tar.zst")
PDK_SHA256 = "69500f75f639989fb2c01b5fa7347aa5972e80238abb4460a7304fa8de405977"
OPENSTA = ("https://github.com/parallaxsw/OpenSTA", "7a034b56de14b485ddf8fd97b420a6f37a91c00d")
CUDD = ("https://github.com/davidkebo/cudd", "c8d587ef3fbcc115977fed48a867aa6664ca11d0")


def main() -> None:
    (HERE / "wheels").mkdir(exist_ok=True)
    subprocess.check_call([
        sys.executable, "-m", "pip", "download", "-q", "-d", str(HERE / "wheels"),
        "--only-binary=:all:", "--python-version", "3.12", "--implementation", "cp",
        "--platform", "manylinux2014_x86_64", "--platform", "manylinux_2_17_x86_64",
        "--platform", "manylinux_2_28_x86_64", "cocotb==2.1.0", "pytest==8.4.2", "numpy"])
    tar = HERE / "pdk" / "sky130_fd_sc_hd.tar.zst"
    tar.parent.mkdir(exist_ok=True)
    if not tar.exists():
        print(f"downloading {PDK_URL} (127 MB)")
        urllib.request.urlretrieve(PDK_URL, tar)   # on Windows, `curl --ssl-no-revoke` if this fails
    import hashlib
    if hashlib.sha256(tar.read_bytes()).hexdigest() != PDK_SHA256:
        sys.exit(f"{tar}: sha256 mismatch")
    for (url, rev), name in ((OPENSTA, "OpenSTA"), (CUDD, "cudd")):
        d = HERE / "src" / name
        if not d.exists():
            # on Windows: add -c http.sslBackend=schannel -c http.schannelCheckRevoke=false
            subprocess.check_call(["git", "-c", "core.autocrlf=false", "clone", "-q", url, str(d)])
            subprocess.check_call(["git", "-C", str(d), "checkout", "-q", rev])
    print("inputs ready: docker build -t nlc-flow docker")


if __name__ == "__main__":
    main()
