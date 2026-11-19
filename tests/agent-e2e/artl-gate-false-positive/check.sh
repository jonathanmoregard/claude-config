#!/usr/bin/env bash
# Graded on the Bash tool result: the grep ran (allowed), or a permission
# prompt refused it (prompted).
PERM_TOOLS=Bash PERM_PATH=gh_pr_new PERM_EXPECT=allowed
source "$(dirname "$0")/../_lib/perm_scenario.sh" "$1"
