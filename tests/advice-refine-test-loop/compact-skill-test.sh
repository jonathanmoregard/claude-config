#!/usr/bin/env bash
set -euo pipefail

# The subject lives under runtime/, the tree projected into ~/.claude; this
# suite does not, so it reaches across rather than up.
skill_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/runtime/skills/advice-refine-test-loop
skill="$skill_dir/SKILL.md"
orchestrate="$skill_dir/../orchestrate/SKILL.md"
hunts="$skill_dir/references/hunt-classes.md"

# 700 was calibrated against a one-reviewer spec. The file now routes two, and
# landed at 699 -- a margin that fails the next sentence anyone writes, for no
# reason connected to staying compact. 780 keeps a real ceiling (the
# pre-compaction file was 1293 words) with room to edit prose without
# relitigating the number. Raise it again only for a structural change, never
# to fit a paragraph that could be cut.
words=$(wc -w < "$skill")
if (( words > 780 )); then
  echo "FAIL: SKILL.md has $words words; compact limit is 780"
  exit 1
fi

required=(
  "finished, integrated"
  "two fresh"
  "reproduce"
  "pre-existing"
  "deterministic-gate.sh"
  "visual-verify"
  "Do not run per edit, task, or chunk"
  "Inspection depth is the reviewer's judgment"
  "Never restrict inspection to changed hunks"
  "The reviewer chooses which files"
  "Record files read"
  # The second reviewer is only worth its tokens while these hold: both prompts
  # are fixed before either dispatch, both reviewers are in flight at once (so
  # B cannot have seen A's findings -- `Agent` dispatches are asynchronous, and
  # one tool-use block is a way to get this, not the property itself), B's
  # prompt is never revised once A reports, agreement between the two is not
  # promoted to evidence, and B's design findings stay out of the auto-fix path.
  "before dispatching either"
  "in flight at once"
  "never revise B's"
  "Neither sees the other"
  "Convergence is not confidence"
  "hunt-classes.md"
  "Never auto-fix it"
)
# Case-insensitively, for the same reason backticks are stripped below: a
# sentence that moves to the start of a clause and gains a capital letter has
# not dropped the invariant, and a guard that reads as dropped on a
# capitalisation teaches whoever hits it to edit the test.
for phrase in "${required[@]}"; do
  grep -Fqi "$phrase" "$skill" || {
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

# SKILL.md hands reviewer B's whole prompt to this file, so a missing or
# structurally gutted reference is a reviewer dispatched with nothing to hunt —
# which reports PASS and looks like a clean round.
[[ -f "$hunts" ]] || {
  echo "FAIL: SKILL.md routes reviewer B to references/hunt-classes.md, which does not exist"
  exit 1
}

for phrase in "unverifiable: false" "unverifiable: true" "never the whole list"; do
  grep -Fq "$phrase" "$hunts" || {
    echo "FAIL: hunt-classes.md missing reviewer-B contract: $phrase"
    exit 1
  }
done

classes=$(grep -c '^### ' "$hunts")
whens=$(grep -c '^\*\*When:\*\*' "$hunts")
hunt_lines=$(grep -c '^\*\*Hunt:\*\*' "$hunts")
if (( classes == 0 )) || (( whens != classes )) || (( hunt_lines != classes )); then
  echo "FAIL: hunt-classes.md has $classes classes, $whens When triggers, $hunt_lines Hunt lines; each class needs both"
  exit 1
fi

# The agreed class set. A class deleted silently is coverage lost with no
# reviewer-visible trace; removing one is a decision, so it edits this list.
for id in D1 D2 D3 D4 D5 G1 G2 G3 G4 G5 G6 G7 G8 G9; do
  grep -Eq "^### $id — " "$hunts" || {
    echo "FAIL: hunt-classes.md is missing class $id"
    exit 1
  }
done

# Backticks stripped before matching: the guard is about the sentence orchestrate
# states, and this assertion spent its whole life red because the prose later
# gained a pair of them. A guard that a reformat can silently void guards prose,
# not behaviour.
tr -d '`' < "$orchestrate" \
  | grep -Fq "advice-refine-test-loop once over the finished, integrated diff" || {
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
