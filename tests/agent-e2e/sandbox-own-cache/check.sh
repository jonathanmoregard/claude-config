#!/usr/bin/env bash
set -uo pipefail
run=${1:?run dir}
seen=$run/seen
[ -f "$seen" ] || { echo "INCONCLUSIVE: probe.sh never ran"; exit 2; }
summary=$(tr '\n' ' ' <"$seen")
grep -qx 'sandbox=1' "$seen" || { echo "INCONCLUSIVE: probe ran outside the sandbox: $summary"; exit 2; }
if grep -q '^xdg=.*/\.cache/claude-sandbox$' "$seen" && grep -q '^cargo=.*/\.cache/claude-sandbox/cargo$' "$seen"; then
  echo "PASS: sandboxed Bash uses its own cache root: $summary"; exit 0
fi
echo "FAIL: sandboxed Bash still points at the shared caches: $summary"; exit 1
