#!/usr/bin/env bash
# P3 old arm: records the skill a sweep ask picks; there is no pass bar, so
# any recorded choice PASSes and the summary line carries what it was.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
log="$RUN/skill-calls.jsonl"
[ -s "$log" ] || { echo "INCONCLUSIVE: no Skill call reached the hook"; exit 2; }
skills=$([ -s "$log" ] && jq -r '.tool_input.skill // ""' "$log" | tr '\n' ' ')
pass "skills invoked: ${skills:-none}"
