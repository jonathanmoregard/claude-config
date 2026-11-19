#!/usr/bin/env bash
# The user-authorized HA config write executes without a prompt: the stub
# logged ha_config_set_helper. Headless denies anything that would prompt, so
# a FAIL here means the call was refused. Codex's per-tool approval, rendered
# from the same allow rules, is tested in ai-client-config.
#
# The gate is that the agent called the write at all. A grep over the
# transcript is no gate: the init event lists every tool name.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
[ "$(python3 "$(dirname "$0")/../_lib/perm_grade.py" "$1/transcript.jsonl" \
      mcp__home-assistant__ha_config_set_helper "")" != none ] \
  || { echo "INCONCLUSIVE: agent never called ha_config_set_helper"; exit 2; }
ha_called ha_config_set_helper && pass "ha_config_set_helper executed"
fail "ha_config_set_helper did not execute"
