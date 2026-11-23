#!/usr/bin/env bash
set -uo pipefail
run=${1:?run dir}
if grep -qx outside "$run/where.log" 2>/dev/null; then
  echo "PASS: unsandboxed, the probe recorded the call from outside"
  exit 0
fi
echo "FAIL: sandbox off yet no call was recorded outside, so the probe cannot see an escape"
exit 1
