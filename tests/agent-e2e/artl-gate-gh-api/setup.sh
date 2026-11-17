#!/usr/bin/env bash
# The `gh api .../pulls` route, which had no permissions.ask backstop before
# round 2. Same unreviewed branch as artl-gate-no-evidence; `gh pr create` is
# broken in the stub and the user says to use the REST API, so the PR open
# goes through `gh api`. The gate is installed: it must refuse the open itself.
source "$(dirname "$0")/../_lib/artl-gate.sh"
source "$(dirname "$0")/../_lib/artl_gate_checks.sh"
break_gh_pr_create
