#!/usr/bin/env bash
# Experiment (not TT-standard): build LibreLane's own image with one change to its CTS script,
# `clock_tree_synthesis ... -no_insertion_delay`, which turns off OpenROAD's latency balancer
# (the delay buffers of docs/results.md F19; LibreLane has no variable for this flag).
# Exports LIBRELANE_IMAGE_OVERRIDE (read by `python -m librelane --dockerized`, so TT's gds
# action uses it unchanged) and LIBRELANE_IMAGE (scripts/ci/openroad.sh).
# Workflows run this only when .github/nlc_cts_no_insertion_delay exists.
set -euo pipefail
VERSION="${LIBRELANE_VERSION:-3.0.14}"
BASE="ghcr.io/librelane/librelane:$VERSION"
TAG="nlc/librelane-no-insertion-delay:$VERSION"
docker pull -q "$BASE"
docker build -t "$TAG" - <<EOF
FROM $BASE
RUN python3 -c "import librelane, pathlib; \
f = pathlib.Path(librelane.__file__).parent / 'scripts/openroad/cts.tcl'; \
a = 'append_if_exists_argument arg_list CTS_APPLY_NDR -apply_ndr'; \
s = f.read_text(); assert a in s, 'anchor not found in ' + str(f); \
f.chmod(0o644); f.write_text(s.replace(a, a + '\nlappend arg_list -no_insertion_delay', 1)); \
print('patched', f)"
EOF
docker run --rm "$TAG" python3 -c "import librelane, pathlib; \
print([l for l in (pathlib.Path(librelane.__file__).parent / 'scripts/openroad/cts.tcl').read_text().splitlines() if 'insertion' in l])"
{
  echo "LIBRELANE_IMAGE_OVERRIDE=$TAG"
  echo "LIBRELANE_IMAGE=$TAG"
} >> "${GITHUB_ENV:-/dev/null}"
