#!/usr/bin/env bash
# Open a PR for the current branch against main, title and body from the commits.
set -euo pipefail
git push -q -u origin HEAD
gh pr create --fill
