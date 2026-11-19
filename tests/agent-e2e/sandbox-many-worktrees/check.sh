#!/usr/bin/env bash
set -uo pipefail
run=${1:?run dir}
if grep -qx ran "$run/stamp.log" 2>/dev/null; then
  echo "PASS: the shell ran with 170 worktrees holding work"
  exit 0
fi
if grep -q 'stamp\.sh' "$run/commands.txt" 2>/dev/null; then
  echo "FAIL: the agent ran stamp.sh and it never executed (the sandbox could not start a shell)"
  exit 1
fi
echo "INCONCLUSIVE: the agent never ran stamp.sh"
exit 2
