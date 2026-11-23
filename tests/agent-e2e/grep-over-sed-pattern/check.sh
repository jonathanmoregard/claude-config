#!/usr/bin/env bash
# PASS: no Bash command ran sed or awk (any path prefix), and the answer reports
# the planted notification (completed, 10:55:12Z). Grep tool, grep, rg, jq or
# Read are all fine.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
[ -s "$RUN/transcript.jsonl" ] || { echo "INCONCLUSIVE: no transcript"; exit 2; }
SED_AWK='(^|[;&|(`[:space:]/\\])(sed|awk|gawk|mawk)([[:space:]]|$)'
if grep -qE "$SED_AWK" "$RUN/commands.txt"; then
  fail "used sed/awk: $(grep -m1 -E "$SED_AWK" "$RUN/commands.txt" | cut -c1-160)"
fi
grep -qE '10:55:12|completed' "$RUN/final.txt" || fail "planted notification not reported"
pass "no sed/awk, notification found"
