#!/usr/bin/env bash
# artl-gate-evidence-present with a tracked symlink among the changed files.
# The review reads it by the link's own path, as a live advisor does; a
# recorder that keeps only the resolved target leaves the link unread.
source "$(dirname "$0")/../_lib/artl-gate.sh"
ln -s retry-loop.sh scripts/retry.sh
git add -A
git commit -qm "feat: retry.sh alias for the wrapper"
git push -q
source "$(dirname "$0")/../_lib/artl-seed-review.sh"
