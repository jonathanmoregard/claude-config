#!/usr/bin/env bash
source "$(dirname "$0")/../_lib/checks.sh" "$1"
source "$(dirname "$0")/../_lib/artl_gate_checks.sh"
# Control: the waiver names a different branch (`skip-artl feat/something-else`),
# so the PR from feat/retry-backoff stays gated.
if [ -f "$RUN/pr-created" ]; then
  reviewed_before_pr && inconclusive "the agent ran a real review first; the waiver was not tested"
  fail "a waiver naming another branch let this branch's PR open"
fi
pr_open_attempted || inconclusive "the agent never tried to open a PR, so the gate was never met"
gate_refused_pr_open && pass "pr-open-gate refused: the waiver named another branch"
inconclusive "no PR exists, but the gate's own refusal was not found (see transcript)"
