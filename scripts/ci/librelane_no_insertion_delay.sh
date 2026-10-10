#!/usr/bin/env bash
# Experiment (not TT-standard): build LibreLane's own image with one change to its CTS script,
# `clock_tree_synthesis ... -no_insertion_delay`, which turns off OpenROAD's latency balancer
# (the delay buffers of docs/results.md F19; LibreLane has no variable for this flag).
# Extra clock_tree_synthesis flags (one line, e.g. `-dont_use_dummy_load`) can be added in
# .github/nlc_cts_extra_args, and a Tcl file .github/nlc_cts_post.tcl is sourced right after
# clock_tree_synthesis (exp G: flat clock below small gates).
# Exports LIBRELANE_IMAGE_OVERRIDE (read by `python -m librelane --dockerized`, so TT's gds
# action uses it unchanged) and LIBRELANE_IMAGE (scripts/ci/openroad.sh).
# Workflows run this only when .github/nlc_cts_no_insertion_delay exists.
set -euo pipefail
VERSION="${LIBRELANE_VERSION:-3.0.14}"
BASE="ghcr.io/librelane/librelane:$VERSION"
TAG="nlc/librelane-no-insertion-delay:$VERSION"
HERE="$(cd "$(dirname "$0")/../.." && pwd)"
EXTRA="-no_insertion_delay"
if [ -f "$HERE/.github/nlc_cts_extra_args" ]; then
  EXTRA="$EXTRA $(grep -v '^#' "$HERE/.github/nlc_cts_extra_args" | tr '\n' ' ' | xargs)"
fi
echo "extra clock_tree_synthesis flags: $EXTRA"
CTX=$(mktemp -d)
if [ -f "$HERE/.github/nlc_cts_post.tcl" ]; then
  cp "$HERE/.github/nlc_cts_post.tcl" "$CTX/nlc_cts_post.tcl"
else
  echo "# no post-CTS step" > "$CTX/nlc_cts_post.tcl"
fi
docker pull -q "$BASE"
docker build -t "$TAG" -f - "$CTX" <<EOF
FROM $BASE
COPY nlc_cts_post.tcl /nlc_cts_post.tcl
RUN python3 -c "import librelane, pathlib; \
f = pathlib.Path(librelane.__file__).parent / 'scripts/openroad/cts.tcl'; \
a = 'append_if_exists_argument arg_list CTS_APPLY_NDR -apply_ndr'; \
b = 'log_cmd clock_tree_synthesis {*}\\\$arg_list'; \
s = f.read_text(); assert a in s and b in s, 'anchor not found in ' + str(f); \
s = s.replace(a, a + '\nlappend arg_list $EXTRA', 1); \
s = s.replace(b, b + '\nsource /nlc_cts_post.tcl', 1); \
f.chmod(0o644); f.write_text(s); \
print('patched', f)"
EOF
docker run --rm "$TAG" python3 -c "import librelane, pathlib; \
print([l for l in (pathlib.Path(librelane.__file__).parent / 'scripts/openroad/cts.tcl').read_text().splitlines() if 'insertion' in l or 'nlc' in l])"
rm -rf "$CTX"
{
  echo "LIBRELANE_IMAGE_OVERRIDE=$TAG"
  echo "LIBRELANE_IMAGE=$TAG"
} >> "${GITHUB_ENV:-/dev/null}"
