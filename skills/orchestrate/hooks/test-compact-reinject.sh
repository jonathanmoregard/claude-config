#!/usr/bin/env bash
# test-compact-reinject.sh — asserts doctrine re-injection behavior.
set -euo pipefail
SCRIPT="$(cd "$(dirname "$0")" && pwd)/compact-reinject.sh"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
cd "$TMP"

# Ralph state is session-keyed in USERSPACE, not a file in the project. Every
# case below therefore redirects HOME and plants state under
# $HOME/.local/state/claude-ralph-loop/, keyed by `armed_in:` = this cwd.
# HOME redirection is also what makes these assertions independent of the real
# home directory's contents.
HOME_ARMED="$TMP/home-armed"
RALPH_DIR="$HOME_ARMED/.local/state/claude-ralph-loop"
mkdir -p "$RALPH_DIR"
STATE="$RALPH_DIR/test-session-0000.md"

arm() {  # $1=iteration  $2=completion_promise-yaml  $3=started_at
  cat > "$STATE" <<EOF
---
active: true
iteration: $1
session_id: test-session-0000
armed_in: "$TMP"
max_iterations: 25
completion_promise: $2
started_at: "$3"
---
Autonomous mission: read tasks/mission.md and continue.
EOF
}

run() { HOME="${1:-$HOME_ARMED}" bash "$SCRIPT"; }

# Case 1: no state file anywhere → empty output, exit 0
OUT=$(HOME="$TMP/empty-home" bash "$SCRIPT")
[[ -z "$OUT" ]] || { echo "FAIL: expected empty output without state file"; exit 1; }

# Case 2: armed state file (fresh) → doctrine with iteration + mission pointer,
# NO literal promise text (doctrine must never print the sigil — parroting footgun,
# and the plugin's own systemMessage already shows it), NO staleness warning.
#
# `tasks/` here is the migrated shape: notes live in ~/.local/state/claude-tasks/<repo>/ and the
# project symlinks tasks/ at them, so the repo-relative spelling still resolves. The
# assertions below therefore also pin that a repo-local tasks/ wins over userspace.
mkdir -p tasks
: > tasks/mission.md
NOW_TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
arm 7 '"All mission.md criteria verified green"' "$NOW_TS"
OUT=$(run)
echo "$OUT" | grep -q 'iteration 7'                                || { echo "FAIL: missing iteration"; exit 1; }
echo "$OUT" | grep -q 'tasks/mission.md'                           || { echo "FAIL: missing mission.md pointer"; exit 1; }
echo "$OUT" | grep -q 'tasks/session-constraints.md'               || { echo "FAIL: missing session-constraints pointer"; exit 1; }
echo "$OUT" | grep -qi 'never end a turn'                          || { echo "FAIL: missing bias-to-action doctrine"; exit 1; }
echo "$OUT" | grep -qF 'All mission.md criteria verified green'    && { echo "FAIL: doctrine must not print literal sigil"; exit 1; }
echo "$OUT" | grep -q 'WARNING'                                    && { echo "FAIL: fresh loop must not warn stale"; exit 1; }

# Case 2b: the hook must not create anything inside the project. This is the
# regression that started the migration — a ralph file written into a public
# repo's worktree, where a sync cron auto-commits whatever it finds.
[[ ! -e .claude ]] || { echo "FAIL: hook created .claude/ inside the project"; exit 1; }

# Case 3: promise unset (null) → same static doctrine, no <promise> tag rendered
arm 2 'null' "2026-08-02T00:00:00Z"
OUT=$(run)
echo "$OUT" | grep -q 'iteration 2'    || { echo "FAIL: missing iteration (null promise case)"; exit 1; }
echo "$OUT" | grep -qF '<promise>'     && { echo "FAIL: null-promise doctrine must not render a <promise> tag"; exit 1; }

# Case 4: stale loop (started_at 2020) → staleness WARNING naming the cancel path
arm 3 '"x"' "2020-01-01T00:00:00Z"
OUT=$(run)
echo "$OUT" | grep -q 'WARNING'          || { echo "FAIL: stale loop must warn"; exit 1; }
echo "$OUT" | grep -qF '/cancel-ralph'   || { echo "FAIL: stale warning must include cancel command"; exit 1; }
echo "$OUT" | grep -qF "$STATE"          || { echo "FAIL: stale warning must name the userspace state file"; exit 1; }

# Case 5: corrupted iteration field with command substitution → sanitized to '?', nothing executed
CANARY="$TMP/canary"
arm "\$(touch $CANARY)" '"x"' "2020-01-01T00:00:00Z"
OUT=$(run)
echo "$OUT" | grep -q 'iteration ?' || { echo "FAIL: corrupted iteration must render as ?"; exit 1; }
[[ ! -e "$CANARY" ]]                || { echo "FAIL: state-file content was EXECUTED"; exit 1; }

# Case 5b: a loop armed in ANOTHER directory must not gate this one. State is
# keyed by session and matched on armed_in:, so a stray file in the state dir is
# not this project's loop.
arm 9 '"x"' "$NOW_TS"
sed -i 's|^armed_in: .*|armed_in: "/nonexistent/elsewhere"|' "$STATE"
OUT=$(run)
echo "$OUT" | grep -q 'iteration 9' && { echo "FAIL: a loop armed elsewhere gated this directory"; exit 1; }
echo "$OUT" | grep -q 'context-lifecycle' || { echo "FAIL: expected the mission-only reminder; got: $OUT"; exit 1; }
rm -f "$STATE"

# Case 6: notes in USERSPACE only, no repo-relative tasks/ — the un-migrated shape,
# and the one this hook used to be blind to. A project whose notes live at
# ~/.local/state/claude-tasks/<repo>/ must still get the reminder, and it must name that path
# rather than a repo-relative one nothing writes to.
rm -rf tasks
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

# Case 8: STALE mission files (old session's leftovers) are not this session's work:
# no mission reminder, in either location. A fresh one alongside revives it.
STALE_HOME="$TMP/home-stale"
STALE_DIR="$STALE_HOME/.local/state/claude-tasks/${TMP##*/}"
mkdir -p "$STALE_DIR"
touch -d '3 days ago' "$STALE_DIR/mission-old-topic.md"
OUT=$(HOME="$STALE_HOME" bash "$SCRIPT")
[[ -z "$OUT" ]] || { echo "FAIL: stale userspace mission produced a reminder; got: $OUT"; exit 1; }
mkdir -p tasks; touch -d '3 days ago' tasks/mission.md
OUT=$(HOME="$STALE_HOME" bash "$SCRIPT")
[[ -z "$OUT" ]] || { echo "FAIL: stale tasks/ mission produced a reminder; got: $OUT"; exit 1; }
: > "$STALE_DIR/mission.md"
OUT=$(HOME="$STALE_HOME" bash "$SCRIPT")
echo "$OUT" | grep -q 'context-lifecycle' || { echo "FAIL: fresh mission beside stale ones gave no reminder"; exit 1; }

# Case 9: an armed ralph loop still gets its doctrine when every mission file is stale.
rm -f "$STALE_DIR/mission.md"
arm 4 '"x"' "$NOW_TS"
OUT=$(run)
echo "$OUT" | grep -q 'iteration 4' || { echo "FAIL: armed loop lost its doctrine with stale missions"; exit 1; }
rm -f "$STATE"; rm -r tasks

# Case 10: a session-reflect resolved handoff counts regardless of file age.
RES_HOME="$TMP/home-resolved"
mkdir -p "$RES_HOME/.claude/skills/session-reflect"
: > "$TMP/handoff-mission.md"; touch -d '3 days ago' "$TMP/handoff-mission.md"
printf '#!/usr/bin/env bash\necho "%s"\n' "$TMP/handoff-mission.md" > "$RES_HOME/.claude/skills/session-reflect/reflect.sh"
OUT=$(HOME="$RES_HOME" bash "$SCRIPT")
echo "$OUT" | grep -qF "$TMP/handoff-mission.md" || { echo "FAIL: resolved handoff gave no reminder; got: $OUT"; exit 1; }

echo "PASS"
