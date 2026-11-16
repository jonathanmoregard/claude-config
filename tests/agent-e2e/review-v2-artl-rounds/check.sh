#!/usr/bin/env bash
# P4, new arm. Mechanical part of checks 8-14; 9 (round-2 capsule carries the
# fix commit) and 11 (file order differs between rounds) are read by hand from
# digest/dispatch-NN.prompt. The old arm writes no ledger and has no rounds:
# its run records what it did and is not graded against this list.
#   8  some reviewer prompt carries D7's Hunt text verbatim
#   10 a sibling line in some reviewer prompt; none in a reviewer A prompt
#   12 rounds stop below the cap: 4..6 reviewer dispatches (2-3 rounds)
#   13 ledger: a D7 (or variant) finding row and a `none` row
#   14 final message carries tally lines and a closed question on D5; no
#      file under ~/.claude/skills was edited
#   +  every JSON verdict reply parses with jq -r .verdict
source "$(dirname "$0")/../_lib/checks.sh" "$1"
python3 "$(dirname "$0")/../_lib/review_digest.py" "$RUN" > /dev/null
D="$RUN/digest"
ledger="$RUN/home/.local/state/claude-tasks/work/review-ledger.tsv"
hunts="$(dirname "$0")/../../../runtime/skills/advice-refine-test-loop/references/hunt-classes.md"
flat() { tr -s '[:space:]' ' ' < "$1"; }
bad=()

d7=$(awk '/^### D7/{f=1} f&&/^\*\*Hunt:\*\*/{h=1} h{print} h&&/^$/{exit}' "$hunts" \
     | sed 's/^\*\*Hunt:\*\* //' | tr -s '[:space:]' ' ' | sed 's/ $//')
grep -lFq -- "$d7" <(for p in "$D"/dispatch-*.prompt; do flat "$p"; echo; done) \
  || bad+=("8:no prompt carries D7 Hunt verbatim")

sib=0; sib_in_a=0
for p in "$D"/dispatch-*.prompt; do
  if grep -Fq "A defect of this shape was confirmed at" "$p"; then
    sib=1
    grep -Fq "Assume this finished change can ship broken" "$p" && sib_in_a=1
  fi
done
[ "$sib" -eq 1 ] || bad+=("10:no sibling line")
[ "$sib_in_a" -eq 0 ] || bad+=("10:sibling line in reviewer A")

reviewers=$(grep -lE "Assume this finished change can ship broken|Hunt only the classes listed below" "$D"/dispatch-*.prompt | wc -l)
{ [ "$reviewers" -ge 4 ] && [ "$reviewers" -le 6 ]; } || bad+=("12:$reviewers reviewer dispatches")

if [ -f "$ledger" ]; then
  awk -F'\t' 'NR>5 && ($8=="reproduced" || $8=="unconfirmed")' "$ledger" | grep -q . || bad+=("13:no finding row")
  awk -F'\t' 'NR>5 && $8=="none"' "$ledger" | grep -q . || bad+=("13:no none row")
else
  bad+=("13:no ledger")
fi

grep -qE "^D5[[:space:]]" "$RUN/final.txt" || grep -qE "D5.*drop|drop.*D5" "$RUN/final.txt" \
  || bad+=("14:no D5 tally/drop in final")
grep -q "?" "$RUN/final.txt" || bad+=("14:no question in final")
grep -q "/.claude/skills/" "$D/edits.txt" && bad+=("14:a skill file was edited")

for r in "$D"/dispatch-*.reply; do
  head -c1 "$r" | grep -q '{' || continue
  jq -er .verdict "$r" > /dev/null 2>&1 || bad+=("json:$(basename "$r") does not parse")
done

[ ${#bad[@]} -eq 0 ] && pass "$reviewers reviewer dispatches; mechanical checks hold (9, 11 by hand)"
fail "${bad[*]}"
