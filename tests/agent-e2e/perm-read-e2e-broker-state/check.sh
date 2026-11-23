#!/usr/bin/env bash
# A Read of a file in the agent-e2e broker's state dir (campaign transcripts)
# is allowed without a prompt.
PERM_TOOLS=Read PERM_PATH=/.local/state/agent-e2e-broker/ PERM_EXPECT="allowed"
source "$(dirname "$0")/../_lib/perm_scenario.sh" "$1"
