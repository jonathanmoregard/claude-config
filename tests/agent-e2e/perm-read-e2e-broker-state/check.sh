#!/usr/bin/env bash
# A Read of a file in the agent-e2e broker's state dir (campaign transcripts)
# is allowed without a prompt.
# Graded on probe.txt alone: the workspace (and its target.txt) also sits under
# the broker state dir, inside the cwd, so matching the dir graded that Read.
# setup.sh refuses to run unless probe.txt is under the broker state dir.
PERM_TOOLS=Read PERM_PATH=/probe.txt PERM_EXPECT="allowed"
source "$(dirname "$0")/../_lib/perm_scenario.sh" "$1"
