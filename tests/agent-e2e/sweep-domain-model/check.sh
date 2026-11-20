#!/usr/bin/env bash
# PASS when the sweep's lens files, read line by line, show all of:
#   T  the inventory records the project as personal
#   P1 the preset 0..4 proposed as a LABELLED sum type: a label word (coldest,
#      warmest, ...) on a line about the preset. A bare `0|1|2|3|4` union fails.
#   P2 a planted sentinel (lastPreset's -1, or room === '') on a line that
#      proposes an explicit case or calls it a sentinel. direction()'s -1 for
#      "left" does not count.
#   P3 per-kind id types (RoomId / LampId / branded) on a line that is not a
#      SKIP: rejecting them as ceremony fails.
#   P4 the Home Assistant names or service-call list named as adapter work
#      inside the core.
#   P5 `Plan` renamed with the README's own words ("lighting state").
#   X  the Venues lens ran, and no RECOMMEND line proposes alerting,
#      observability, metrics, tracing, restore rehearsals, leak checks or
#      growth measurement.
#   and the fixture's tree is untouched.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
dir="$RUN/home/.local/state/claude-tasks/work/assurance-sweep"
[ -d "$dir" ] || fail "no assurance-sweep output dir"
st=$(git -C "$RUN/work" status --porcelain)
[ -z "$st" ] || fail "tree changed: $(head -c 80 <<<"$st")"
out=$(python3 "$(dirname "$0")/grade.py" "$dir")
case "$out" in
  PASS*) pass "${out#PASS }" ;;
  *) fail "$out" ;;
esac
