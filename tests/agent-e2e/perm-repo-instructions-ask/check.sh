#!/usr/bin/env bash
# Every later session in the repo loads CLAUDE.md as instructions, so it keeps
# prompting inside an allow-listed checkout: the ask beats the repo's Edit allow.
PERM_TOOLS=Write,Edit PERM_PATH=/home/jonathan/Repos/dependency-guard/CLAUDE.md PERM_EXPECT=prompted
source "$(dirname "$0")/../_lib/perm_scenario.sh" "$1"
