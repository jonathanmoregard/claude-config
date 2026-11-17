#!/usr/bin/env bash
# The main checkouts that must never be edited in place stay closed under the
# Repos allow. Only the explicit deny on ~/Repos/nixos-config/** passes: the
# allow covers the path, so a prompt here would mean the deny is gone.
PERM_TOOLS=Write,Edit PERM_PATH=/home/jonathan/Repos/nixos-config/E2E_SCRATCH.md PERM_EXPECT=deny-rule
source "$(dirname "$0")/../_lib/perm_scenario.sh" "$1"
