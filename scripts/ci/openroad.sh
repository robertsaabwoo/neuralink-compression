#!/usr/bin/env bash
# NLC_STA for the cts_preview workflow: run a Tcl script in OpenROAD from LibreLane's container
# (the image the LibreLane run already pulled), with the workspace and the PDK (~/.ciel)
# mounted at the same paths, so the flow's absolute paths work unchanged.
#   NLC_STA=scripts/ci/openroad.sh python scripts/flow/flow.py --only layout
set -euo pipefail
exec docker run --rm --user "$(id -u):$(id -g)" \
  -v "${GITHUB_WORKSPACE:-$PWD}:${GITHUB_WORKSPACE:-$PWD}" \
  -v "$HOME/.ciel:$HOME/.ciel" \
  -w "$PWD" "${LIBRELANE_IMAGE:?set LIBRELANE_IMAGE}" \
  openroad -no_init -no_splash -exit "$@"
