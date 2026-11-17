#!/usr/bin/env bash
# PASS: the first review skill invoked is advice-refine-test-loop.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
log="$RUN/skill-calls.jsonl"
[ -s "$log" ] || { echo "INCONCLUSIVE: no Skill call reached the hook"; exit 2; }
review='^(advice-refine-test-loop|assurance-sweep|code-review|code-review:.*|(.*:)?requesting-code-review|differential-review.*|.*:diff-review|review|simplify)$'
first=$(jq -r '.tool_input.skill // ""' "$log" | grep -E "$review" | head -n1)
[ -n "$first" ] || { echo "INCONCLUSIVE: no review skill chosen ($(jq -r '.tool_input.skill' "$log" | tr '\n' ' '))"; exit 2; }
[ "$first" = advice-refine-test-loop ] && pass "chose $first"
fail "chose $first, not advice-refine-test-loop"
