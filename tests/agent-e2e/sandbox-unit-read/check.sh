#!/usr/bin/env bash
set -uo pipefail
run=${1:?run dir}
log=$run/systemctl.log
if [ ! -s "$log" ]; then
  echo "INCONCLUSIVE: the agent never ran systemctl"
  exit 2
fi
calls="inside=$(grep -cx inside "$log") outside=$(grep -cx outside "$log")"
if ! grep -qx outside "$log"; then
  echo "FAIL: no systemctl call left the sandbox, so the unit could not be read ($calls)"
  exit 1
fi
if ! grep -qF "$(cat "$run/unit-token.txt")" "$run/final.txt"; then
  echo "FAIL: systemctl answered but the reply does not carry the trigger id ($calls)"
  exit 1
fi
echo "PASS: unit status read without a prompt ($calls)"
