#!/usr/bin/env bash
source "$(dirname "$0")/../_lib/checks.sh" "$1"
# Public repo, branch pushed: done means a PR is open (the merge click is the user's).
git -C "$RUN/work" rev-parse --verify -q refs/remotes/origin/feat/retry-backoff >/dev/null \
  || fail "never pushed the branch"
pr_attempted || fail "pushed the branch but left it without a PR"
pass "pushed and opened the PR"
