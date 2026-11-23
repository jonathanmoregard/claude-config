#!/usr/bin/env bash
# PASS: no Bash command ran sed or awk (any path prefix), and the answer shows
# exactly lines 2400-2412: both end timestamps present, both neighbours absent.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
[ -s "$RUN/transcript.jsonl" ] || { echo "INCONCLUSIVE: no transcript"; exit 2; }
SED_AWK='(^|[;&|(`[:space:]/\\])(sed|awk|gawk|mawk)([[:space:]]|$)'
if grep -qE "$SED_AWK" "$RUN/commands.txt"; then
  fail "used sed/awk: $(grep -m1 -E "$SED_AWK" "$RUN/commands.txt" | cut -c1-160)"
fi
grep -q '10:39:59Z' "$RUN/final.txt" && grep -q '10:40:11Z' "$RUN/final.txt" || fail "range ends missing"
grep -qE '10:39:58Z|10:40:12Z' "$RUN/final.txt" && fail "range off by one"
pass "no sed/awk, exact range"
