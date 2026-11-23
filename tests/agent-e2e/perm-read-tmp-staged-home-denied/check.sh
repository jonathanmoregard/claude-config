#!/usr/bin/env bash
# A Read inside a staged home/ of a direct harness run under /tmp is refused by
# a deny rule, not merely prompted: deny is what holds even though
# Read(//tmp/**) matches.
PERM_TOOLS=Read PERM_PATH=/old/1/home/.claude/backups/ PERM_EXPECT="deny-rule"
source "$(dirname "$0")/../_lib/perm_scenario.sh" "$1"
