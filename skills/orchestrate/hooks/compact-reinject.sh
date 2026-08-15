#!/usr/bin/env bash
# SessionStart(startup|compact|clear) hook: when a ralph loop is armed in this
# project dir, re-inject the autonomy contract — doctrine decays after compaction.
# Design constraints (advisor-reviewed):
# - NEVER print the literal sigil (parroting footgun; plugin systemMessage
#   already shows it each iteration; also avoids the null-promise nonsense case).
# - State-file values are DATA: iteration is regex-validated before heredoc
#   interpolation so a corrupted file cannot execute via $(…) expansion.
set -euo pipefail

STATE=".claude/ralph-loop.local.md"

# EXISTENCE CHECK ONLY, and deliberately a GLOB: "does this project use the
# mission convention at all". The files it matches are human-readable copies —
# the authoritative handoff lives outside the repo — so this must never be read
# as content, only counted.
mission_exists() {
  compgen -G "tasks/mission*.md" >/dev/null 2>&1
}

# Name the AUTHORITATIVE handoff when session-reflect can resolve one
# ($REFLECT_ROOT/sessions/<sid>/mission.md, out of repo). Falls back to the
# human-facing `tasks/mission.md` convention when there is none — acceptable here
# and nowhere else: this hook only injects a "re-read your handoff" reminder into
# an interactive session whose human owns this cwd. It never drives an unattended
# fork, which is why idle-handoff.sh has no such fallback.
RESOLVE="$HOME/.claude/skills/session-reflect/reflect.sh"
MISSION="tasks/mission.md"
if [[ -x "$RESOLVE" || -f "$RESOLVE" ]]; then
  RESOLVED="$(bash "$RESOLVE" --mission-path "$PWD" </dev/null 2>/dev/null || true)"
  [[ -n "$RESOLVED" && -f "$RESOLVED" ]] && MISSION="$RESOLVED"
fi

# Two independent reminders. Neither depends on the other; exit 0 if neither
# applies. Order: ralph doctrine first (more constraining), mission second.

if [[ ! -f "$STATE" ]] && ! mission_exists; then
  exit 0
fi

if [[ ! -f "$STATE" ]]; then
  # Mission-only reminder: any project with a mission artifact gets a re-read
  # nudge after compact/clear. Compaction is documented to invalidate the
  # cached prefix that held CLAUDE.md and mission context; disk beats
  # in-context memory once compaction has fired.
  cat <<EOF
[context-lifecycle] A mission handoff exists in this project ($MISSION). Context was just compacted, cleared, or restarted — re-read that file now, plus tasks/session-constraints.md if present. Do not rely on your memory of the pre-compact conversation for state that lives on disk.
EOF
  exit 0
fi

ITER=$(awk '/^iteration:/{sub(/^iteration: */,""); print; exit}' "$STATE")
[[ "$ITER" =~ ^[0-9]+$ ]] || ITER="?"

AGE_NOTE=""
STARTED=$(awk '/^started_at:/{sub(/^started_at: */,""); gsub(/"/,""); print; exit}' "$STATE")
START_EPOCH=$(date -d "${STARTED:-invalid}" +%s 2>/dev/null || echo 0)
if [[ "$START_EPOCH" -gt 0 ]] && (( $(date +%s) - START_EPOCH > 172800 )); then
  AGE_NOTE=$'\n- WARNING: this loop was armed over 48h ago. If the mission is dead, cancel it: rm .claude/ralph-loop.local.md (or /cancel-ralph). A stale armed loop blocks EVERY session that stops in this directory.'
fi

cat <<EOF
[autonomous-orchestration] Ralph loop ACTIVE (iteration ${ITER}). Context was just compacted, cleared, or restarted — re-arm the doctrine:
- Re-read $MISSION, tasks/progress.md, and tasks/session-constraints.md NOW. Constraints are the first thing compaction kills — the file is authoritative. Trust files and git log over any memory of this conversation.
- You are the orchestrator: dispatch subagents for all reads/edits/tests; hold no code context yourself.
- Bias to action. Never end a turn with only a plan, a summary, or a question that mission.md or the dangerous-ops manifest already answers.
- Stop ONLY via the completion sigil recorded in $MISSION, emitted ONLY when literally TRUE: verifier freshly green AND fresh-context reviewer confirmed. Never emit a false sigil. If no sigil is configured, the loop ends only at max-iterations.${AGE_NOTE}
EOF
