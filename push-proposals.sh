#!/usr/bin/env bash
set -euo pipefail
cd ~/.claude
git add recursive-self-improvement/proposals/
if git diff --cached --quiet recursive-self-improvement/proposals/; then
  echo "No proposal changes to commit"
  exit 0
fi
git commit -m "improvement proposals: $(date +%Y-%m-%d)"
git push
