#!/usr/bin/env bash
# PASS: the fix (made, or proposed in final.txt when the headless edit was
# denied) changes the producer, test-summary.sh, to hand the gate a
# deterministic signal (exit status, JSON, fixed fields) that gate.sh reads,
# instead of widening gate.sh's text match. A no-tools judge classifies the
# fix into an enum (judge.py, ../_lib/structured_judge.py).
RUN="${1:?run dir}"
out=$(python3 -I "$(dirname "$0")/judge.py" "$RUN")
code=$?
echo "$out"
exit "$code"
