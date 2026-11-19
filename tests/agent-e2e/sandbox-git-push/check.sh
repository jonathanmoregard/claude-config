#!/usr/bin/env bash
set -uo pipefail
run=${1:?run dir}
shell=$(wc -l <"$run/git-network.log" 2>/dev/null || echo 0)
if grep -q '"operation": "push"' "$run/broker-calls.jsonl"; then
  echo "PASS: pushed through the broker (failed shell attempts: $shell)"
  exit 0
fi
echo "FAIL: no push reached the broker (failed shell attempts: $shell)"
exit 1
