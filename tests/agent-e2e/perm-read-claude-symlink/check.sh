#!/usr/bin/env bash
# ~/.claude/agents is a symlink into ~/.claude/runtime. An allow must match
# both the link and its target: Read(~/.claude/agents/**) covers
# the link, Read(~/.claude/runtime/**) the target.
PERM_TOOLS=Read PERM_PATH=/home/jonathan/.claude/agents/advisor.md PERM_EXPECT=allowed
source "$(dirname "$0")/../_lib/perm_scenario.sh" "$1"
