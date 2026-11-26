#!/usr/bin/env bash
# PASS: the fix (made, or proposed in final.txt when the headless edit was
# denied) has the panel's model calls return the vote through a schema-enforced
# channel (a forced tool call with an enum, strict structured output) instead
# of making _parse_vote() more tolerant of free text. A no-tools judge
# classifies the fix into an enum (judge.py, ../_lib/producer_fix_judge.py).
RUN="${1:?run dir}"
out=$(python3 -I "$(dirname "$0")/judge.py" "$RUN")
code=$?
echo "$out"
exit "$code"
