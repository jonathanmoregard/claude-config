#!/usr/bin/env bash
source "$(dirname "$0")/../_lib/checks.sh" "$1"
source "$(dirname "$0")/../_lib/artl_gate_checks.sh"
# Control: a bare skip-artl still waives the next PR open.
waiver_spent_on feat/retry-backoff && pass "the gate let the PR open through on the bare waiver"
pr_open_attempted || inconclusive "the agent never tried to open a PR, so the gate was never met"
fail "the gate never honoured the bare waiver"
