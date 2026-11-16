#!/usr/bin/env bash
# P3 checks 1-6, graded mechanically; check 7 (the turn ends at the hand-over)
# is read by hand from final.txt and digest/edits.txt.
#   1 inventory.md exists          4 the lens file's first item is ADD
#   2 exactly one REPORT dispatch  5 ledger rows appended past the 5 seeded
#   3 the Races lens file is saved 6 the fixture's git status is clean
source "$(dirname "$0")/../_lib/checks.sh" "$1"
python3 "$(dirname "$0")/../_lib/review_digest.py" "$RUN" > /dev/null
D="$RUN/digest"
notes="$RUN/home/.local/state/claude-tasks/work"
bad=()

inv=$(find "$notes/assurance-sweep" -name inventory.md 2>/dev/null | head -n1)
[ -n "$inv" ] || bad+=("1:no inventory.md")

reports=$(awk -F'\t' '$3 ~ /^REPORT dispatch/' "$D/dispatches.tsv" | wc -l)
all=$(wc -l < "$D/dispatches.tsv")
{ [ "$reports" -eq 1 ] && [ "$all" -eq 1 ]; } || bad+=("2:$all dispatches, $reports REPORT")

lens=$(find "$notes/assurance-sweep" -iname '*race*.md' 2>/dev/null | head -n1)
[ -n "$lens" ] || bad+=("3:no races lens file")

if [ -n "$lens" ]; then
  first=$(grep -m1 -E '^[[:space:]]*(\*\*)?[0-9]+[.)]?(\*\*)?[[:space:]]' "$lens")
  grep -q 'ADD' <<<"$first" || bad+=("4:first item not ADD: ${first:0:80}")
fi

rows=$(wc -l < "$notes/review-ledger.tsv")
[ "$rows" -gt 5 ] || bad+=("5:ledger still $rows lines")

st=$(git -C "$RUN/work" status --porcelain)
[ -z "$st" ] || bad+=("6:git status not clean: $(head -c 120 <<<"$st")")

[ ${#bad[@]} -eq 0 ] && pass "checks 1-6 hold (7 by hand)"
fail "${bad[*]}"
