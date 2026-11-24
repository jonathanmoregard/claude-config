#!/usr/bin/env bash
# P4, new arm. The docvault diff is T2 (artl_gate.py tier), so round 1 is the
# blind pair and every later round is one reviewer over the delta. Check 9
# (the round-2 capsule carries the fix commit, not the whole diff) is read by
# hand from digest/dispatch-NN.prompt; artl-delta-round grades it mechanically.
# The old arm ran other round rules: its run is a record, not graded here.
#   8  some reviewer prompt carries D7's Hunt text verbatim
#   10 B's prompt only in round 1: every reviewer after the first two carries
#      A's prompt alone and a "Do not report:" list
#   12 2..4 reviewer dispatches (a pair, then at most two delta rounds under
#      the cap of three); at least 3 when round 1 reproduced and fixed a finding
#   13 ledger: a D7 (or variant) finding row and a `none` row
#   14 final message carries tally lines and a closed question on D5; no
#      file under ~/.claude/skills was edited
#   15 a review-runs.tsv row for the close-out
source "$(dirname "$0")/../_lib/checks.sh" "$1"
python3 "$(dirname "$0")/../_lib/review_digest.py" "$RUN" > /dev/null
D="$RUN/digest"
notes="$RUN/home/.local/state/claude-tasks/work"
ledger="$notes/review-ledger.tsv"
hunts="$(dirname "$0")/../../../runtime/skills/advice-refine-test-loop/references/hunt-classes.md"
flat() { tr -s '[:space:]' ' ' < "$1"; }
A="Assume this finished change can ship broken"
B="Hunt only the classes listed below"
bad=()

d7=$(awk '/^### D7/{f=1} f&&/^\*\*Hunt:\*\*/{h=1} h{print} h&&/^$/{exit}' "$hunts" \
     | sed 's/^\*\*Hunt:\*\* //' | tr -s '[:space:]' ' ' | sed 's/ $//')
[ -n "$d7" ] || fail "8:could not read D7's Hunt text from hunt-classes.md"
grep -lFq -- "$d7" <(for p in "$D"/dispatch-*.prompt; do flat "$p"; echo; done) \
  || bad+=("8:no prompt carries D7 Hunt verbatim")

revs=()
for p in "$D"/dispatch-*.prompt; do
  [ -e "$p" ] || continue
  flat "$p" | grep -Fq -e "$A" -e "$B" && revs+=("$p")
done
n=${#revs[@]}
for p in "${revs[@]:2}"; do
  flat "$p" | grep -Fq "$B" && bad+=("10:$(basename "$p") is a reviewer B after round 1")
  flat "$p" | grep -Fqi "Do not report" || bad+=("10:$(basename "$p") has no Do-not-report list")
done

{ [ "$n" -ge 2 ] && [ "$n" -le 4 ]; } || bad+=("12:$n reviewer dispatches")
if [ -f "$ledger" ] && awk -F'\t' 'NR>5 && $3 ~ /\/r1$/ && $8=="reproduced" && $9=="yes"' "$ledger" | grep -q .; then
  [ "$n" -ge 3 ] || bad+=("12:round 1 reproduced and fixed a finding, but no round 2 ran")
fi

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

awk -F'\t' 'NF>=8 && $3=="T2"' "$notes/review-runs.tsv" 2>/dev/null | grep -q . \
  || bad+=("15:no review-runs.tsv row with tier T2")

[ ${#bad[@]} -eq 0 ] && pass "$n reviewer dispatches; mechanical checks hold (9 by hand)"
fail "${bad[*]}"
