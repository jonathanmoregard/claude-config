#!/usr/bin/env bash
# Public repo, finished work on a local branch that was never pushed, no PR.
source "$(dirname "$0")/../_lib/completion.sh" --prs
git push -q origin --delete feat/retry-backoff
git branch -q --unset-upstream
