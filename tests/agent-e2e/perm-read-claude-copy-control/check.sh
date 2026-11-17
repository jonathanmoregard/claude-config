#!/usr/bin/env bash
# Regression for review round 1: a broad Read(~/.claude/**) allow exposed
# copies of the denied .claude.json (sandbox homes under jobs/, backups/) and
# paste-cache. The allow list names config entries only, so this stays closed.
PERM_TOOLS=Read PERM_PATH=/home/jonathan/.claude/jobs/ PERM_EXPECT="prompted deny-rule"
source "$(dirname "$0")/../_lib/perm_scenario.sh" "$1"
