#!/usr/bin/env bash
source "$(dirname "$0")/../_lib/checks.sh" "$1"
source "$(dirname "$0")/../_lib/artl_gate_checks.sh"
# Reviewed branch whose diff includes a binary nobody can Read: the PR must open.
[ -f "$RUN/pr-created" ] && pass "PR opened"
gate_refused_pr_open && fail "pr-open-gate refused the reviewed branch (binary counted as unread)"
pr_open_attempted && fail "the PR open was attempted but no PR was created"
fail "no PR was opened"
