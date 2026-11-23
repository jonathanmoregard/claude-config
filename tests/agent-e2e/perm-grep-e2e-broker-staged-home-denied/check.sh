#!/usr/bin/env bash
# The Read deny on staged home/ dirs under the agent-e2e broker state dir also
# refuses Grep: Grep is blanket-allowed, so a deny that only bound Read would
# leave the staged credentials searchable.
PERM_TOOLS=Grep PERM_PATH=/fixture/home/.claude/backups/ PERM_EXPECT="deny-rule"
source "$(dirname "$0")/../_lib/perm_scenario.sh" "$1"
