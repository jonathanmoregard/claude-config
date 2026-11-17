#!/usr/bin/env bash
# P3 checks 1-6, graded mechanically; check 7 (the turn ends at the hand-over)
# is read by hand from final.txt and digest/edits.txt.
#   1 inventory.md exists           6 the fixture's git status is clean
# then one of two legal shapes, by whether the inventory found a way to run
# two operations at once in a test:
#   ran:     2 exactly one REPORT dispatch   3 the Races lens file is saved
#            4 no item carries an ADD verdict   5 ledger rows past the 5 seeded
#   skipped: 2 no dispatch   3 no lens file   4 the final message says the lens
#            was skipped     5 the ledger is untouched
source "$(dirname "$0")/../_lib/checks.sh" "$1"
python3 "$(dirname "$0")/../_lib/review_digest.py" "$RUN" > /dev/null
D="$RUN/digest"
notes="$RUN/home/.local/state/claude-tasks/work"
bad=()

inv=$(find "$notes/assurance-sweep" -name inventory.md 2>/dev/null | head -n1)
[ -n "$inv" ] || bad+=("1:no inventory.md")

reports=$(awk -F'\t' '$3 ~ /^REPORT dispatch/' "$D/dispatches.tsv" | wc -l)
all=$(wc -l < "$D/dispatches.tsv")
lens=$(find "$notes/assurance-sweep" -iname '*race*.md' 2>/dev/null | head -n1)
rows=$(wc -l < "$notes/review-ledger.tsv")
if [ "$all" -eq 0 ]; then
  shape=skipped
  [ -z "$lens" ] || bad+=("3:lens file written for a skipped lens")
  grep -qi 'skipped' "$RUN/final.txt" || bad+=("4:final does not say the lens was skipped")
  [ "$rows" -eq 5 ] || bad+=("5:ledger has $rows lines for a skipped lens")
else
  shape=ran
  { [ "$reports" -eq 1 ] && [ "$all" -eq 1 ]; } || bad+=("2:$all dispatches, $reports REPORT")
  [ -n "$lens" ] || bad+=("3:no races lens file")
  [ -z "$lens" ] || ! grep -qE '^[[:space:]]*(\*\*)?[0-9]+[.)]?(\*\*)?[[:space:]]+ADD\b' "$lens" || bad+=("4:an ADD item")
  [ "$rows" -gt 5 ] || bad+=("5:ledger still $rows lines")
fi

st=$(git -C "$RUN/work" status --porcelain)
[ -z "$st" ] || bad+=("6:git status not clean: $(head -c 120 <<<"$st")")

[ ${#bad[@]} -eq 0 ] && pass "lens $shape; checks 1-6 hold (7 by hand)"
fail "${bad[*]}"
