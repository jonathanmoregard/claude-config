#!/usr/bin/env bash
# Empty git repo with a jobs/ dir; the agent writes jobs/inbox-digest.sh.
set -euo pipefail
WORK="${E2E_WORK:?}"
mkdir -p "$WORK/jobs"
printf '# jobs\nScheduled scripts for this machine.\n' > "$WORK/jobs/README.md"
git -C "$WORK" init -q
git -C "$WORK" add -A
git -C "$WORK" -c user.email=e2e@example.invalid -c user.name=e2e commit -qm init
