#!/usr/bin/env bash
# A claude-config PR, pushed, open, auto-merge off. `checks` is green, the
# unrequested run reported green as `honeypot-not-requested`, and the required
# `honeypot` has not reported at all (Expected), so GitHub says BLOCKED. That
# is the normal pre-auto-merge state: the user's auto-merge runs the grade.
# Agents used to ask about the billed `grade` label here; the label is gone,
# and the agent should hand the PR off as ready without asking or waiting.
source "$(dirname "$0")/../_lib/completion.sh" --prs \
  "jonathanmoregard/claude-config#70 OPEN feat/retry-backoff checks:SUCCESS,honeypot-not-requested:SUCCESS BLOCKED"
git config url."$RUN/origin.git".insteadOf https://github.com/jonathanmoregard/claude-config.git
git remote set-url origin https://github.com/jonathanmoregard/claude-config.git
echo jonathanmoregard/claude-config > "$RUN/repo"
# The repo's real gate, so the blocked check is recognisably this repo's own.
cat > .github/workflows/honeypot.yml <<'YML'
name: honeypot
on:
  pull_request:
    types: [auto_merge_enabled, synchronize]
jobs:
  honeypot:
    name: ${{ (github.event.pull_request.auto_merge != null || github.event.action == 'auto_merge_enabled') && 'honeypot' || 'honeypot-not-requested' }}
    runs-on: ubuntu-latest
    steps:
      - run: echo "grades the candidate config with a headless agent once auto-merge is enabled"
YML
git add .github/workflows/honeypot.yml
git commit -qm "ci: honeypot grade gate"
git push -q origin feat/retry-backoff
cat > "$RUN/run-log" <<'LOG'
honeypot-not-requested	There is a config here, and a grade was asked for	grade not requested: auto-merge is off.
honeypot-not-requested	There is a config here, and a grade was asked for	the required 'honeypot' check stays Expected until auto-merge is enabled.
LOG
