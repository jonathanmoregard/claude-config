#!/usr/bin/env bash
# The broad ~/.claude read allow does not open the prompt history: the deny on
# history.jsonl wins.
PERM_TOOLS=Read PERM_PATH=/home/jonathan/.claude/history.jsonl PERM_EXPECT="prompted deny-rule"
source "$(dirname "$0")/../_lib/perm_scenario.sh" "$1"
