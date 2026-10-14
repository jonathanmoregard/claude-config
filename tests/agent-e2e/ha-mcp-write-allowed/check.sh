#!/usr/bin/env bash
# The user-authorized HA config write executes without a prompt: the stub
# logged ha_config_set_helper. Headless denies anything that would prompt, so
# a FAIL here means the call was refused. Codex's per-tool approval, rendered
# from the same allow rules, is tested in ai-client-config.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
ha_called ha_get_state || { echo "INCONCLUSIVE: agent never used the HA MCP server"; exit 2; }
ha_called ha_config_set_helper && pass "ha_config_set_helper executed"
fail "ha_config_set_helper did not execute"
