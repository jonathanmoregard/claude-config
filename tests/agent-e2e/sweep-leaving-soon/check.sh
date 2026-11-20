#!/usr/bin/env bash
# PASS when the sweep's inventory lists legacy-sync.ts (to be deleted) under
# "Leaving soon" and lists none of the re-typing targets (room and lamp ids,
# the preset), and a Domain model lens file exists. Python runs isolated (-I):
# HOME here is the run's, and its user site-packages are writable.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
dir="$RUN/home/.local/state/claude-tasks/work/assurance-sweep"
[ -d "$dir" ] || fail "no assurance-sweep output dir"
out=$(python3 -I "$(dirname "$0")/grade_leaving.py" "$dir") || fail "$out"
ls "$dir"/*/ | grep -qi 'domain' || fail "no Domain model lens file"
pass "$out; lens 3 file present"
