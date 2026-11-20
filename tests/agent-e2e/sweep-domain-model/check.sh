#!/usr/bin/env bash
# PASS when the sweep's lens files, read item by item (an item runs from its
# number to the next), show all of the below. Proposals count only from an
# item's change text (its "Change:" bullet, the change segment of a one-line
# item, or a table row), in an item that is not SKIP and does not call the
# idea ceremony; file paths and code comments never count.
#   T  the inventory's Tier line says personal
#   P1 the preset 0..4 proposed as a LABELLED union ('coldest' | 'cold' | ...).
#      A bare `0|1|2|3|4` union fails.
#   P2 lastPreset's -1 or room '' given an explicit case ('none-seen',
#      | undefined, a kind: case).
#   P3 per-kind id types defined (type RoomId, LampId =, parseRoomId).
#   P4 the change hands the Home Assistant entity and service names to the
#      adapter.
#   P5 a change line or vocabulary row puts the README's "lighting state" in
#      place of `Plan`.
#   G  an item titled as the guard: a type-checker test that also rejects casts.
#   X  Venues ran (and marks something n/a) or final.txt says it was skipped,
#      and no line of a non-SKIP item proposes alerting, observability,
#      metrics, tracing, restore rehearsals, leak checks or growth measurement.
#   and the fixture's tree is untouched.
# grade_selftest.py pins every check against the false PASSes review found.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
dir="$RUN/home/.local/state/claude-tasks/work/assurance-sweep"
[ -d "$dir" ] || fail "no assurance-sweep output dir"
st=$(git -C "$RUN/work" status --porcelain)
[ -z "$st" ] || fail "tree changed: $(head -c 80 <<<"$st")"
out=$(python3 "$(dirname "$0")/grade.py" "$dir" "$(cat "$RUN/final.txt" 2>/dev/null)")
case "$out" in
  PASS*) pass "${out#PASS }" ;;
  *) fail "$out" ;;
esac
