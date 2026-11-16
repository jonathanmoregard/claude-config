#!/usr/bin/env bash
# The situation the argv gate targets: a PR opened by a route the text rules
# cannot see. The checkout's own helper script (open-pr.sh here) runs gh's
# PR create; the Bash command is `./scripts/open-pr.sh`, which no
# permissions.ask glob matches and which pr-open-gate's PreToolUse scan never
# sees (no `gh` word in it). Same unreviewed, non-exempt branch as
# artl-gate-no-evidence.
source "$(dirname "$0")/../_lib/artl-gate.sh"
cp "$(dirname "$0")/open-pr.sh" scripts/open-pr.sh
chmod +x scripts/open-pr.sh
git add -A
git commit -qm "chore: PR helper"
git push -q
