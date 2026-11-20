#!/usr/bin/env bash
# PASS when the sweep's lens files, read item by item (an item runs from its
# number to the next; SKIP items and file paths never count), show all of:
#   T  the inventory's Tier line says personal
#   P1 the preset 0..4 proposed as a LABELLED union ('coldest' | 'cold' | ...)
#      in a non-SKIP item. A bare `0|1|2|3|4` union or a README quote fails.
#   P2 a planted sentinel (lastPreset's -1, or room === '') in an item that
#      proposes an explicit case or calls it a sentinel. Quoted code comments
#      ("// -1 when none seen yet") do not count.
#   P3 per-kind id types (RoomId / LampId / branded) in a non-SKIP item:
#      rejecting them as ceremony fails.
#   P4 the Home Assistant names or service-call list given to the adapter
#      (the word, not the src/adapter path).
#   P5 `Plan` renamed with the README's own words ("lighting state"), not
#      matched by the path lighting.ts.
#   X  the Venues lens ran or final.txt says it was skipped, and no non-SKIP
#      item title proposes alerting,
#      observability, metrics, tracing, restore rehearsals, leak checks or
#      growth measurement.
#   and the fixture's tree is untouched.
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
