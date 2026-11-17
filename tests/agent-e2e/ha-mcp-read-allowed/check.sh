#!/usr/bin/env bash
# A read-only HA tool, covered by the `mcp__home-assistant__ha_get_*` allow
# glob, executes without a prompt. Headless denies anything that would prompt,
# so a FAIL means the glob did not match.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
grep -q "mcp__home-assistant__ha_get_state" "$1/transcript.jsonl" \
  || { echo "INCONCLUSIVE: agent never called ha_get_state"; exit 2; }
ha_called ha_get_state && pass "ha_get_state executed"
fail "ha_get_state did not execute"
