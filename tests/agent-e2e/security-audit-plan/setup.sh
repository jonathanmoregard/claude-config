#!/usr/bin/env bash
# Small mixed-surface repo (Python web handler, NixOS module, GitHub workflow)
# copied from fixture/ into the workspace and committed.
set -euo pipefail
WORK="${E2E_WORK:?}"
cp -r "$(dirname "$0")/fixture/." "$WORK/"
git -C "$WORK" init -q
git -C "$WORK" add -A
git -C "$WORK" -c user.email=e2e@example.invalid -c user.name=e2e commit -qm init
