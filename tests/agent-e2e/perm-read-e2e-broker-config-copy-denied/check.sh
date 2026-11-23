#!/usr/bin/env bash
# A Read of a .claude.json copy under the agent-e2e broker state dir is
# refused by a deny rule, not merely prompted: deny is what holds even though
# the allow for that dir matches.
PERM_TOOLS=Read PERM_PATH=/staged/.claude.json PERM_EXPECT="deny-rule"
source "$(dirname "$0")/../_lib/perm_scenario.sh" "$1"
