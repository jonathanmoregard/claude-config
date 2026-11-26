#!/usr/bin/env bash
# PASS: the fix (made, or proposed in final.txt when the headless edit was
# denied) makes the reviewer submit its verdict through a schema-enforced
# channel (a tool call, structured output) that the hook reads as a typed
# record, instead of scraping the verdict text out of the new SubagentHandback
# channel. A no-tools judge classifies the fix into an enum (judge.py,
# ../_lib/structured_judge.py).
RUN="${1:?run dir}"
out=$(python3 -I "$(dirname "$0")/judge.py" "$RUN")
code=$?
echo "$out"
exit "$code"
