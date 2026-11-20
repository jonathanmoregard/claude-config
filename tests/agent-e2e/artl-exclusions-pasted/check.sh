#!/usr/bin/env bash
# PASS: every reviewer prompt in the close-out review carries "Do not report:"
# with the exclusions.md entry. No reviewer at all is INCONCLUSIVE: both
# arms instruct a dispatch, so zero is a flow fault, not evidence. Python runs
# isolated (-I): HOME here is the run's.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
[ -s "$RUN/transcript.jsonl" ] || { echo "INCONCLUSIVE: no transcript"; exit 2; }
out=$(python3 -I "$(dirname "$0")/grade_excl.py" "$RUN/transcript.jsonl"); code=$?
case $code in 0) pass "$out" ;; 1) fail "$out" ;; esac
echo "INCONCLUSIVE: $out"; exit 2
