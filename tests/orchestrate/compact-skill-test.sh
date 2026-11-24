#!/usr/bin/env bash
set -euo pipefail

# The subject lives under runtime/, the tree projected into ~/.claude; this
# suite does not, so it reaches across rather than up.
skill_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/runtime/skills/orchestrate
skill="$skill_dir/SKILL.md"
global="$skill_dir/../../CLAUDE.md"

words=$(wc -w < "$skill")
if (( words > 1000 )); then
  echo "FAIL: SKILL.md has $words words; compact budget is 1000"
  exit 1
fi

required=(
  "completion notification"
  "end the turn"
  "tasks/mission.md"
  "tasks/progress.md"
  "tasks/session-constraints.md"
  "first unchecked acceptance criterion"
  "targeted check"
  "full gate"
  "visual-verify"
  '`advice-refine-test-loop` once'
  "finishing-up"
  "readiness checks"
  "durable follow-up"
  "Use judgment"
)
for phrase in "${required[@]}"; do
  grep -Fq "$phrase" "$skill" || {
    echo "FAIL: missing orchestration invariant: $phrase"
    exit 1
  }
done

obsolete=(
  "two-stage spec"
  "Parent holds NO code context"
  "Every chunk ships blackbox tests"
  "--max-iterations 25"
  "multi-lens parallel reviewers"
  "Waiting PRs"
  "report cap ~15 lines"
  # The Ralph stop-hook loop is removed: it re-prompted the session every few
  # seconds while a background subagent ran, burning iterations on nothing.
  "Ralph"
  "ralph-loop"
  "completion promise"
  "sigil"
  "MAX_ITERATIONS"
)
for phrase in "${obsolete[@]}"; do
  if grep -Fqi -- "$phrase" "$skill"; then
    echo "FAIL: obsolete mandatory machinery remains: $phrase"
    exit 1
  fi
done

grep -Fq "Substantial multi-part or unattended work" "$global" || {
  echo "FAIL: CLAUDE.md still has the broad orchestration trigger"
  exit 1
}

if grep -Fq "Never implement inline" "$global"; then
  echo "FAIL: CLAUDE.md still forbids delegation judgment"
  exit 1
fi

echo "PASS: compact orchestrate contract ($words words)"
