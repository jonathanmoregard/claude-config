#!/usr/bin/env bash
# The user-authorized HA config write executes without a prompt: the stub
# logged ha_config_set_helper. Headless denies anything that would prompt, so
# a FAIL here means the call was refused. Codex's per-tool approval, rendered
# from the same allow rules, is tested in tests/codex/.
#
# Only a permission refusal is a FAIL. No call (a transcript grep is no gate:
# the init event lists every tool name), an MCP error or a grader error is
# INCONCLUSIVE.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
ha_called ha_config_set_helper && pass "ha_config_set_helper executed"
grade=$(python3 "$(dirname "$0")/../_lib/perm_grade.py" "$1/transcript.jsonl" \
          mcp__home-assistant__ha_config_set_helper "")
case "$grade" in
  prompted|deny-rule) fail "ha_config_set_helper refused: $grade" ;;
  *) echo "INCONCLUSIVE: ha_config_set_helper outcome '${grade:-grader error}', stub logged no call"; exit 2 ;;
esac
