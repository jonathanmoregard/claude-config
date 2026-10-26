#!/usr/bin/env bash
set -euo pipefail

skill_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
skill="$skill_dir/SKILL.md"
orchestrate="$skill_dir/../orchestrate/SKILL.md"

words=$(wc -w < "$skill")
if (( words > 700 )); then
  echo "FAIL: SKILL.md has $words words; compact limit is 700"
  exit 1
fi

required=(
  "finished, integrated"
  "one fresh"
  "reproduce"
  "pre-existing"
  "deterministic-gate.sh"
  "visual-verify"
  "Do not run per edit, task, or chunk"
  "Inspection depth is the reviewer's judgment"
  "Never restrict inspection to changed hunks"
  "The reviewer chooses which files"
  "Record files read"
)
for phrase in "${required[@]}"; do
  grep -Fq "$phrase" "$skill" || {
    echo "FAIL: missing compact-loop invariant: $phrase"
    exit 1
  }
done

for obsolete in "Phase 1" "Phase 2" "Phase 3" "Phase 4" \
                "ui-taste-phases.md" "codex-advise.sh" \
                "equivalence-judge" "consolidator sub-loop" \
                "one agent per finding" "at most five production files" \
                "five-file bound" "Read each selected production file top-to-bottom"; do
  if grep -Fq "$obsolete" "$skill"; then
    echo "FAIL: obsolete default-path machinery remains: $obsolete"
    exit 1
  fi
done

grep -Fq "advice-refine-test-loop once over the finished, integrated diff" \
  "$orchestrate" || {
    echo "FAIL: orchestrate does not constrain ARTL to one integrated close-out"
    exit 1
  }

for obsolete in "multi-lens parallel reviewers" \
                "consolidated adversarial re-review"; do
  if grep -Fq "$obsolete" "$orchestrate"; then
    echo "FAIL: orchestrate retains duplicate review fan-out: $obsolete"
    exit 1
  fi
done

echo "PASS: compact ARTL contract ($words words)"
