#!/usr/bin/env bash
# The prompt history stays unreadable: no allow names it, and the absolute
# deny on history.jsonl backs that up if a later allow ever reaches it.
PERM_TOOLS=Read PERM_PATH=/home/jonathan/.claude/history.jsonl PERM_EXPECT="prompted deny-rule"
source "$(dirname "$0")/../_lib/perm_scenario.sh" "$1"
