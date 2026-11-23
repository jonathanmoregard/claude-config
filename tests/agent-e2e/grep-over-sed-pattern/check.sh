#!/usr/bin/env bash
# PASS: no Bash command ran sed against the file, and the answer reports the
# notification. Grep tool, grep, rg, jq or Read are all fine.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
[ -s "$RUN/transcript.jsonl" ] || { echo "INCONCLUSIVE: no transcript"; exit 2; }
if grep -qE '(^|[;&|(`[:space:]])sed([[:space:]]|$)' "$RUN/commands.txt"; then
  fail "used sed: $(grep -m1 -E '(^|[;&|(`[:space:]])sed([[:space:]]|$)' "$RUN/commands.txt" | cut -c1-160)"
fi
grep -qiE 'notification' "$RUN/final.txt" || fail "no answer about the notification"
pass "no sed"
