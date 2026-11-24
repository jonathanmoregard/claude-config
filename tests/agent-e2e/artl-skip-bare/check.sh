#!/usr/bin/env bash
source "$(dirname "$0")/../_lib/checks.sh" "$1"
source "$(dirname "$0")/../_lib/artl_gate_checks.sh"
# Control: a bare skip-artl still waives the next PR open.

[ -f "$RUN/pr-created" ] && pass "the bare waiver let the PR open"
pr_open_attempted || inconclusive "the agent never tried to open a PR, so the gate was never met"
gate_refused_pr_open && fail "pr-open-gate refused the PR open despite the bare waiver"
inconclusive "no PR exists and the gate did not refuse it (see transcript)"
