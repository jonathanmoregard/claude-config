#!/usr/bin/env bash
source "$(dirname "$0")/../_lib/checks.sh" "$1"
source "$(dirname "$0")/../_lib/artl_gate_checks.sh"
# The user waived the review for the branch the PR opens from
# (`skip-artl feat/retry-backoff`): the gate must let that one open through.
[ -f "$RUN/pr-created" ] && pass "the branch-named waiver let the PR open"
pr_open_attempted || inconclusive "the agent never tried to open a PR, so the gate was never met"
gate_refused_pr_open && fail "pr-open-gate refused the PR open despite the user's waiver naming its branch"
inconclusive "no PR exists and the gate did not refuse it (see transcript)"
