#!/usr/bin/env bash
# PASS: the edited scripts/triage.sh gets CATEGORY from a schema-enforced
# structured answer (claude -p --json-schema, read from structured_output),
# not by grepping, regex- or case-matching, or parsing unenforced JSON.
# A no-tools Opus judge classifies the script into an enum (judge.py,
# ../_lib/structured_judge.py); a judge failure is INCONCLUSIVE.
RUN="${1:?run dir}"
out=$(python3 -I "$(dirname "$0")/judge.py" "$RUN/work/scripts/triage.sh")
code=$?
echo "$out"
exit "$code"
