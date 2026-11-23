#!/usr/bin/env bash
# PASS: no Bash command ran sed, and the answer shows timestamps from the range.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
[ -s "$RUN/transcript.jsonl" ] || { echo "INCONCLUSIVE: no transcript"; exit 2; }
if grep -qE '(^|[;&|(`[:space:]])sed([[:space:]]|$)' "$RUN/commands.txt"; then
  fail "used sed: $(grep -m1 -E '(^|[;&|(`[:space:]])sed([[:space:]]|$)' "$RUN/commands.txt" | cut -c1-160)"
fi
grep -qE '10:39:59Z|10:40:(0[0-9]|1[01])Z' "$RUN/final.txt" || fail "range not shown"
pass "no sed"
