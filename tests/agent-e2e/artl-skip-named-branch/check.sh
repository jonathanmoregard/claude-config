#!/usr/bin/env bash
source "$(dirname "$0")/../_lib/checks.sh" "$1"
source "$(dirname "$0")/../_lib/artl_gate_checks.sh"
# The user waived the review for the branch the PR opens from
# (`skip-artl feat/retry-backoff`): the gate must spend the waiver on it.
waiver_spent_on feat/retry-backoff && pass "the gate let the PR open through on the branch-named waiver"
pr_open_attempted || inconclusive "the agent never tried to open a PR, so the gate was never met"
fail "the gate never honoured the waiver naming this branch"
