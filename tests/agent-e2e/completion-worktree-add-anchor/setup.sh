#!/usr/bin/env bash
# Public repo whose checkout sits on someone else's pushed branch with no PR.
# The agent only adds a scratch worktree from it: that claims the new worktree,
# not the anchor checkout's branch.
source "$(dirname "$0")/../_lib/completion.sh" --prs
