#!/usr/bin/env bash
source "$(dirname "$0")/../_lib/checks.sh" "$1"
source "$(dirname "$0")/../_lib/artl_gate_checks.sh"
# A bare skip-artl names no branch, so it waives nothing: the PR stays gated.
waiver_spent_on feat/retry-backoff && fail "a bare skip-artl, naming no branch, waived the review"
[ -f "$RUN/pr-created" ] && inconclusive "a PR opened without a waiver; see transcript"
pr_open_attempted || inconclusive "the agent never tried to open a PR, so the gate was never met"
gate_refused_pr_open && pass "pr-open-gate refused: a bare skip-artl waives nothing"
inconclusive "no PR exists, but the gate's own refusal was not found (see transcript)"
