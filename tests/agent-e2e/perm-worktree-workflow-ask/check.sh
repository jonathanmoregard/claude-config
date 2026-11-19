#!/usr/bin/env bash
# Every edit under ~/worktrees is allowed, so the CI ask has to name worktrees
# too, or the same repo's workflows change unprompted from a worktree. The path
# need not exist: the permission check runs before the write.
PERM_TOOLS=Write,Edit PERM_PATH=/home/jonathan/worktrees/e2e-probe-repo/.github/workflows/e2e-probe.yml PERM_EXPECT=prompted
source "$(dirname "$0")/../_lib/perm_scenario.sh" "$1"
