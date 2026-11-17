#!/usr/bin/env bash
# Regression for review round 1: a broad Read(~/.claude/**) allow exposed
# private state next to the config (copies of .claude.json, paste-cache, logs).
# The allow list names config entries only, so the daemon log stays closed.
# The permission check runs before the file lookup, so this grades the rule
# even if the log is ever absent.
PERM_TOOLS=Read PERM_PATH=/home/jonathan/.claude/daemon.log PERM_EXPECT="prompted deny-rule"
source "$(dirname "$0")/../_lib/perm_scenario.sh" "$1"
