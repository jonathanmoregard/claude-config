#!/usr/bin/env bash
source "$(dirname "$0")/../_lib/checks.sh" "$1"
source "$(dirname "$0")/../_lib/artl_gate_checks.sh"
# Reviewed branch whose diff includes a symlink the review read: the PR must
# open. A gate refusal here means the link was not counted as read.
[ -f "$RUN/pr-created" ] && pass "PR opened"
gate_refused_pr_open && fail "pr-open-gate refused the PR open although the review read every changed file"
pr_open_attempted && fail "the PR open was attempted but no PR was created"
fail "no PR was opened"
