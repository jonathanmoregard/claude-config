#!/usr/bin/env bash
# Public repo, work pushed, PR already open with a check that failed for
# reasons the agent did not cause. Delivery state has something to say and
# nothing the turn can do about it.
source "$(dirname "$0")/../_lib/completion.sh" --prs "acme/widget#1 OPEN feat/retry-backoff FAILURE"
