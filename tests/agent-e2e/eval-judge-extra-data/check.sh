#!/usr/bin/env bash
# PASS: the fix (made, or proposed in final.txt when the headless edit was
# denied) has the `claude -p` judge return its scores through a schema-enforced
# channel (--json-schema, read from structured_output) instead of a better
# extraction of JSON from free text. A no-tools judge classifies the fix into
# an enum (judge.py, ../_lib/producer_fix_judge.py).
RUN="${1:?run dir}"
out=$(python3 -I "$(dirname "$0")/judge.py" "$RUN")
code=$?
echo "$out"
exit "$code"
