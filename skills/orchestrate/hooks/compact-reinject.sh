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
MISSION="tasks/mission.md"

# Two independent reminders. Neither depends on the other; exit 0 if neither
# applies. Order: ralph doctrine first (more constraining), mission.md second.

if [[ ! -f "$STATE" && ! -f "$MISSION" ]]; then
  exit 0
fi

if [[ ! -f "$STATE" ]]; then
  # Mission-only reminder: any project with tasks/mission.md gets a re-read
  # nudge after compact/clear. Compaction is documented to invalidate the
  # cached prefix that held CLAUDE.md and mission.md context; disk beats
  # in-context memory once compaction has fired.
  cat <<EOF
[context-lifecycle] tasks/mission.md exists in this project. Context was just compacted, cleared, or restarted — re-read it now, plus tasks/session-constraints.md if present. Do not rely on your memory of the pre-compact conversation for state that lives on disk.
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
- Re-read tasks/mission.md, tasks/progress.md, and tasks/session-constraints.md NOW. Constraints are the first thing compaction kills — the file is authoritative. Trust files and git log over any memory of this conversation.
- You are the orchestrator: dispatch subagents for all reads/edits/tests; hold no code context yourself.
- Bias to action. Never end a turn with only a plan, a summary, or a question that mission.md or the dangerous-ops manifest already answers.
- Stop ONLY via the completion sigil recorded in tasks/mission.md, emitted ONLY when literally TRUE: verifier freshly green AND fresh-context reviewer confirmed. Never emit a false sigil. If no sigil is configured, the loop ends only at max-iterations.${AGE_NOTE}
EOF
