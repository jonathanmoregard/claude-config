#!/usr/bin/env bash
# An allow-listed ~/Repos checkout accepts an in-place edit without a prompt.
# The sandbox lends ~/Repos read-only, so the write ends in EROFS after the
# permission layer said yes, and the real checkout is untouched.
PERM_TOOLS=Write,Edit PERM_PATH=/home/jonathan/Repos/dependency-guard/E2E_NOTES.md PERM_EXPECT=allowed
source "$(dirname "$0")/../_lib/perm_scenario.sh" "$1"
