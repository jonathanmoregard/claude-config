#!/usr/bin/env bash
# A read-only HA tool, covered by the `mcp__home-assistant__ha_get_*` allow
# glob, executes without a prompt. PASS needs the stub to log the call; FAIL
# needs a permission refusal on it. Anything else proves nothing.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
ha_called ha_get_state && pass "ha_get_state executed"
verdict=$(python3 "$(dirname "$0")/../_lib/perm_grade.py" \
  "$1/transcript.jsonl" mcp__home-assistant__ha_get_state "") \
  || { echo "INCONCLUSIVE: could not parse transcript"; exit 2; }
case "$verdict" in
  prompted|deny-rule) fail "ha_get_state refused: $verdict" ;;
esac
echo "INCONCLUSIVE: ha_get_state neither executed nor refused ($verdict)"; exit 2
