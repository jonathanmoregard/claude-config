#!/usr/bin/env bash
# artl-gate-evidence-present plus a pushed deletion: the branch drops the old
# CI workflow that origin/main carries. The seeded passing review reads every
# file still present, as a live advisor would; the deleted one has nothing
# left to Read. The PR must still open.
source "$(dirname "$0")/../_lib/artl-gate.sh"
git rm -q .github/workflows/ci.yml
git commit -qm "ci: drop the repo workflow; the org-level template runs the tests"
git push -q
source "$(dirname "$0")/../_lib/artl-seed-review.sh"
