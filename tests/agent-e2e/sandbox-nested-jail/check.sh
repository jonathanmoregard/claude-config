#!/usr/bin/env bash
set -uo pipefail
run=${1:?run dir}
if ! grep -q 'nested\.sh' "$run/commands.txt" 2>/dev/null; then
  echo "INCONCLUSIVE: the agent never ran nested.sh"
  exit 2
fi
if ! grep -qx 'nested=ok confined=1' "$run/work/nested.log" 2>/dev/null; then
  echo "FAIL: nested.sh never got its inner sandbox started inside a jail"
  exit 1
fi
if ! grep -qF "$(cat "$run/nested-token.txt")" "$run/final.txt"; then
  echo "FAIL: the inner sandbox started but the reply does not carry the run id"
  exit 1
fi
echo "PASS: the nested sandbox ran through the confined runner, without a prompt"
