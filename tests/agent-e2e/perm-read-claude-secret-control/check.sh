#!/usr/bin/env bash
# Account state stays unreadable, and by the absolute deny, not merely by no
# allow naming it: a later allow must not reach it.
PERM_TOOLS=Read PERM_PATH=/home/jonathan/.claude/.claude.json PERM_EXPECT=deny-rule
source "$(dirname "$0")/../_lib/perm_scenario.sh" "$1"
