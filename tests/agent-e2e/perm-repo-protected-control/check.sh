#!/usr/bin/env bash
# The main checkouts that must never be edited in place stay closed under the
# new Repos allow: the deny on ~/Repos/nixos-config/** wins.
PERM_TOOLS=Write,Edit PERM_PATH=/home/jonathan/Repos/nixos-config/E2E_SCRATCH.md PERM_EXPECT="prompted deny-rule"
source "$(dirname "$0")/../_lib/perm_scenario.sh" "$1"
