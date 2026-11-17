#!/usr/bin/env bash
# Edit(//home/jonathan/Repos/**) lets an in-place edit of a ~/Repos checkout
# through. A read-only sandbox mount error still counts: permission said yes.
PERM_TOOLS=Write,Edit PERM_PATH=/home/jonathan/Repos/e2e-perm-probe/NOTES.md PERM_EXPECT=allowed
source "$(dirname "$0")/../_lib/perm_scenario.sh" "$1"
