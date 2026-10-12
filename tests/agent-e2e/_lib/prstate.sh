#!/usr/bin/env bash
# Shared setup for pr-state scenarios: the PR-state gh stub plus a states file.
# Usage from setup.sh:  source "$(dirname "$0")/../_lib/prstate.sh" "owner/repo#n STATE" ...
set -euo pipefail
RUN="${E2E_RUN:?}"; BIN="${E2E_BIN:?}"
LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cp "$LIB/gh-prstate" "$BIN/gh"; chmod +x "$BIN/gh"
: > "$RUN/gh.log"
printf '%s\n' "$@" > "$RUN/pr-states"
