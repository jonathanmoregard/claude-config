#!/usr/bin/env bash
# Seeds a habit cache that is three days stale, so the wellbeing habit-nudge
# hook prints its "cache is stale ... re-auth via ..." warning to the agent.
set -euo pipefail
WORK="${E2E_WORK:?}"
mkdir -p "$WORK" "$HOME/.claude/tmp"
today="$(date +%F)"
old="$(date -d "-3 days" +%F)"
printf '{"ts": 0, "date": "%s", "default_nudge_hour": 18, "habits": []}\n' "$old" > "$HOME/.claude/tmp/.habit-cache.json"
echo "$today $old" > "${E2E_RUN:?}/dates"
