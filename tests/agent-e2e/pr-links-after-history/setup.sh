#!/usr/bin/env bash
# Four open, green PRs across two repos and one already merged: the state the
# staged conversation in prompt.md ends in.
#
# Why this scenario exists: pr-links-on-merge-ask and pr-links-many-prs both
# score at ceiling on the old arm, so neither can see a rule that makes passing
# more likely. Both prompts elicit the status-report template, which puts links
# at the bottom by construction. The reported lapse looked different: a long
# session, the links already given a few messages earlier in a correct status
# reply, then a short question answered in ordinary prose with bare numbers.
#
# The harness runs one user turn, so the history is staged inside prompt.md as
# a continued-session transcript (route (a) in the e2e docs), not replayed.
# pr-links-no-history is the same fixture and the same final question with the
# history removed: the contrast that says whether the history is what matters.
#
# Every claim the history makes is something the live stub agrees with, so an
# agent that checks state (finishing-up asks for a same-turn `gh pr view`)
# finds nothing to contradict and stays on the question. The code changes the
# history mentions are on branches that do not exist locally, so `git log`
# cannot contradict them either.
source "$(dirname "$0")/../_lib/completion.sh" --prs \
  "acme/widget#37 OPEN feat/retry-backoff" \
  "acme/widget#38 MERGED feat/timeout-tuning" \
  "acme/widget#41 OPEN feat/log-sampling" \
  "acme/infra#393 OPEN feat/alert-thresholds" \
  "acme/infra#394 OPEN feat/dashboard-ids"
