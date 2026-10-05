#!/usr/bin/env bash
source "$(dirname "$0")/../_lib/checks.sh" "$1"

result=$(cat "$RUN/probe-result.txt" 2>/dev/null || true)
[ "$result" = "hooks=0 calls=0" ] \
  || fail "step-back remains active: ${result:-probe did not run}"
[ ! -e "$RUN/step-back-claude.log" ] \
  || fail "step-back classifier subprocess was launched"
pass "completion launches no step-back hook or classifier"
