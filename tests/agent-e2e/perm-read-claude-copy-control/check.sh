#!/usr/bin/env bash
# Regression for review round 1: a broad Read(~/.claude/**) allow exposed
# private state next to the config (copies of .claude.json, paste-cache, logs).
# The allow list names config entries only, so a private file stays closed.
# The target is the daemon log, which persists; if it is ever missing the run
# grades INCONCLUSIVE, never PASS.
PERM_TOOLS=Read PERM_PATH=/home/jonathan/.claude/daemon.log PERM_EXPECT="prompted deny-rule"
source "$(dirname "$0")/../_lib/perm_scenario.sh" "$1"
