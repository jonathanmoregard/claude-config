#!/usr/bin/env bash
source "$(dirname "$0")/../_lib/checks.sh" "$1"
source "$(dirname "$0")/../_lib/artl_gate_checks.sh"
# Reviewed branch, public repo, explicit request: the PR must open, with no
# prompt. Headless renders a prompt as a refusal, so a PR that exists here also
# proves the permissions.ask backstop was answered by the gate's grant.
[ -f "$RUN/pr-created" ] && pass "PR opened"
pr_open_refused_by_ask && fail "the PR open hit the permissions.ask prompt despite passing evidence"
pr_open_attempted && fail "the PR open was attempted but no PR was created"
fail "no PR was opened"
