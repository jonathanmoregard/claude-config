#!/usr/bin/env bash
# A harmless grep whose text holds "gh", "pr" and "new" in that order. The old
# permissions.ask glob Bash(*gh*pr*new*) prompted on it (headless: refused);
# with the PR-open gate hook-only, it is not a PR open and must just run.
# The prompt's `wc -l` is a control: a run where every Bash call is refused is
# INCONCLUSIVE in the harness, so the old arm needs one call that runs.
set -euo pipefail
cat > "${E2E_WORK:?}/notes.txt" <<'TXT'
counters
gh_pr_new: 42
gh_pr_merged: 17
TXT
