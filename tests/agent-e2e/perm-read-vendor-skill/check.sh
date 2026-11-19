#!/usr/bin/env bash
# A vendored skill is two hops away: ~/.claude/skills -> runtime/skills, whose
# entry links into ~/.claude/vendor/superpowers. The allow needs the link and
# the final target, so Read(//home/jonathan/.claude/vendor/**) must be there.
PERM_TOOLS=Read PERM_PATH=/home/jonathan/.claude/skills/test-driven-development/SKILL.md PERM_EXPECT=allowed
source "$(dirname "$0")/../_lib/perm_scenario.sh" "$1"
