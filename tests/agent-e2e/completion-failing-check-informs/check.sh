#!/usr/bin/env bash
source "$(dirname "$0")/../_lib/checks.sh" "$1"
# Graded on forcing, not on prose: the delivery-state hook may report the
# failing check, it may never send the turn back over it.
git -C "$RUN/origin.git" show feat/retry-backoff:src/retry.py 2>/dev/null | grep -q 'cap=60' \
  || { echo "INCONCLUSIVE: the bump never reached origin (the task itself did not happen)"; exit 2; }
grep -q 'Completion check: work this session touched is not finished' "$RUN/transcript.jsonl" \
  && fail "the delivery-state hook sent the turn back over the failing check"
pass "pushed; the failing check informed without forcing the turn back"
