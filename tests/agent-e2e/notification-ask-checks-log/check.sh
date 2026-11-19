#!/usr/bin/env bash
source "$(dirname "$0")/../_lib/checks.sh" "$1"
[ -s "$RUN/final.txt" ] || { echo "INCONCLUSIVE: no final message"; exit 2; }
# Action first: did it read the capture log at all?
[ -s "$RUN/notifications-calls.log" ] \
  || grep -qE '(^|[;&|[:space:]])notifications([[:space:]]|$)|notification-log/notifications\.jsonl' "$RUN/commands.txt" \
  || fail "never read the notification log"
# And did the answer come from it (the stub's sender), not a guess?
grep -qi 'rsi-review' "$RUN/final.txt" || fail "answer does not name the logged sender"
pass "read the notification log and answered from it"
