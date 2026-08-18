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
#
# `tasks/` here is the migrated shape: notes live in ~/.local/state/claude-tasks/<repo>/ and the
# project symlinks tasks/ at them, so the repo-relative spelling still resolves. The
# assertions below therefore also pin that a repo-local tasks/ wins over userspace.
mkdir -p .claude tasks
: > tasks/mission.md
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
echo "$OUT" | grep -q 'tasks/session-constraints.md'               || { echo "FAIL: missing session-constraints pointer"; exit 1; }
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

# Case 6: notes in USERSPACE only, no repo-relative tasks/ — the un-migrated shape,
# and the one this hook used to be blind to. A project whose notes live at
# ~/.local/state/claude-tasks/<repo>/ must still get the reminder, and it must name that path
# rather than a repo-relative one nothing writes to.
# HOME is redirected for this case only, so the assertion cannot depend on the real
# home directory's contents.
rm -rf .claude tasks
FAKE_HOME="$TMP/home"
mkdir -p "$FAKE_HOME/.local/state/claude-tasks/${TMP##*/}"
: > "$FAKE_HOME/.local/state/claude-tasks/${TMP##*/}/mission.md"
OUT=$(HOME="$FAKE_HOME" bash "$SCRIPT")
echo "$OUT" | grep -q 'context-lifecycle' \
  || { echo "FAIL: userspace-only notes produced no reminder"; exit 1; }
echo "$OUT" | grep -qF "$FAKE_HOME/.local/state/claude-tasks/${TMP##*/}/mission.md" \
  || { echo "FAIL: reminder did not name the userspace mission path; got: $OUT"; exit 1; }
echo "$OUT" | grep -qE '(^| )tasks/(mission|session-constraints)\.md' \
  && { echo "FAIL: reminder named a repo-relative notes path that nothing writes to"; exit 1; }

# Case 7: no notes anywhere and no state file → still silent (case 1's invariant must
# survive the userspace fallback: naming a path is not the same as inventing one).
OUT=$(HOME="$TMP/empty-home" bash "$SCRIPT")
[[ -z "$OUT" ]] || { echo "FAIL: expected silence with no notes anywhere; got: $OUT"; exit 1; }

echo "PASS"
