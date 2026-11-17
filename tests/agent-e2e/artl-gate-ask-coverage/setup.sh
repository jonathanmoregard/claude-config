#!/usr/bin/env bash
# The real Claude Code permission matcher against the ordinary PR-open
# spellings, and against gh api calls that cannot open a PR (which must run),
# with pr-open-gate removed from the config entirely. The unit tests model
# the matcher (tests/test_pr_open_gate.py::_ask_matches); this runs it. The
# `gh` stub records a PR for `gh pr create` / `gh pr new` and for a gh api
# POST to a pulls collection or createPullRequest (_lib/artl-gate.sh).
source "$(dirname "$0")/../_lib/artl-gate.sh"
source "$(dirname "$0")/../_lib/artl_gate_checks.sh"
remove_pr_open_gate_hook
