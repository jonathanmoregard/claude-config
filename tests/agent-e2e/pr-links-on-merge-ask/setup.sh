#!/usr/bin/env bash
# Finished work on a pushed branch whose PR is open and green, so the only
# thing left is the user's merge click — the moment this rule governs.
#
# Same fixture as completion-green-pr-control, deliberately: that scenario
# already proves no false block fires here, so a FAIL in this one is about the
# shape of the ask and nothing else. The PR's URL is NOT in the prompt (the
# control's prompt carries it), because a scenario that hands the agent the
# link cannot tell a rule being followed from a string being echoed back. The
# gh stub serves it via `pr view --json url`; the agent has to go and get it.
source "$(dirname "$0")/../_lib/completion.sh" --prs "acme/widget#1 OPEN feat/retry-backoff"
