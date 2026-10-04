#!/usr/bin/env bash
# Same mixed-surface repo as security-audit-plan; the question is only about
# where a slow scanner belongs.
set -euo pipefail
WORK="${E2E_WORK:?}"
cp -r "$(dirname "$0")/../security-audit-plan/fixture/." "$WORK/"
git -C "$WORK" init -q
git -C "$WORK" add -A
git -C "$WORK" -c user.email=e2e@example.invalid -c user.name=e2e commit -qm init
