#!/usr/bin/env bash
# Control for tests-out-of-projection: the tool's own documentation moved
# nowhere, and the lines about where its suite lives sit in the same file. An
# agent asked an ordinary question about the tool must still answer it from the
# projection, without going looking for a checkout.
set -euo pipefail
WORK="${E2E_WORK:?}"
git -C "$WORK" init -q
printf 'notes\n' > "$WORK/README.md"
git -C "$WORK" add -A
git -C "$WORK" -c user.email=e2e@example.invalid -c user.name=e2e commit -qm init
