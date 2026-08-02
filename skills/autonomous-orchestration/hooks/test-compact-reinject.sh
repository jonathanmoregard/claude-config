#!/usr/bin/env bash
# test-compact-reinject.sh — asserts doctrine re-injection behavior.
set -euo pipefail
SCRIPT="$(cd "$(dirname "$0")" && pwd)/compact-reinject.sh"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
cd "$TMP"

# Case 1: no state file → empty output, exit 0
OUT=$(bash "$SCRIPT")
[[ -z "$OUT" ]] || { echo "FAIL: expected empty output without state file"; exit 1; }

# Case 2: armed state file (fresh) → doctrine with iteration + mission pointer,
# NO literal promise text (doctrine must never print the sigil — parroting footgun,
# and the plugin's own systemMessage already shows it), NO staleness warning.
mkdir -p .claude
NOW_TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
cat > .claude/ralph-loop.local.md <<EOF
---
active: true
iteration: 7
session_id:
max_iterations: 25
completion_promise: "All mission.md criteria verified green"
started_at: "$NOW_TS"
---
Autonomous mission: read tasks/mission.md and continue.
EOF
OUT=$(bash "$SCRIPT")
echo "$OUT" | grep -q 'iteration 7'                                || { echo "FAIL: missing iteration"; exit 1; }
echo "$OUT" | grep -q 'tasks/mission.md'                           || { echo "FAIL: missing mission.md pointer"; exit 1; }
echo "$OUT" | grep -qi 'never end a turn'                          || { echo "FAIL: missing bias-to-action doctrine"; exit 1; }
echo "$OUT" | grep -qF 'All mission.md criteria verified green'    && { echo "FAIL: doctrine must not print literal sigil"; exit 1; }
echo "$OUT" | grep -q 'WARNING'                                    && { echo "FAIL: fresh loop must not warn stale"; exit 1; }

# Case 3: promise unset (null) → same static doctrine, no <promise> tag rendered
cat > .claude/ralph-loop.local.md <<'EOF'
---
active: true
iteration: 2
session_id:
max_iterations: 0
completion_promise: null
started_at: "2026-08-02T00:00:00Z"
---
body
EOF
OUT=$(bash "$SCRIPT")
echo "$OUT" | grep -q 'iteration 2'    || { echo "FAIL: missing iteration (null promise case)"; exit 1; }
echo "$OUT" | grep -qF '<promise>'     && { echo "FAIL: null-promise doctrine must not render a <promise> tag"; exit 1; }

# Case 4: stale loop (started_at 2020) → staleness WARNING with cancel instructions
cat > .claude/ralph-loop.local.md <<'EOF'
---
active: true
iteration: 3
session_id:
max_iterations: 25
completion_promise: "x"
started_at: "2020-01-01T00:00:00Z"
---
body
EOF
OUT=$(bash "$SCRIPT")
echo "$OUT" | grep -q 'WARNING'                       || { echo "FAIL: stale loop must warn"; exit 1; }
echo "$OUT" | grep -qF 'rm .claude/ralph-loop.local.md' || { echo "FAIL: stale warning must include cancel command"; exit 1; }

# Case 5: corrupted iteration field with command substitution → sanitized to '?', nothing executed
CANARY="$TMP/canary"
cat > .claude/ralph-loop.local.md <<EOF
---
active: true
iteration: \$(touch $CANARY)
session_id:
max_iterations: 25
completion_promise: "x"
started_at: "2020-01-01T00:00:00Z"
---
body
EOF
OUT=$(bash "$SCRIPT")
echo "$OUT" | grep -q 'iteration ?' || { echo "FAIL: corrupted iteration must render as ?"; exit 1; }
[[ ! -e "$CANARY" ]]                || { echo "FAIL: state-file content was EXECUTED"; exit 1; }

echo "PASS"
