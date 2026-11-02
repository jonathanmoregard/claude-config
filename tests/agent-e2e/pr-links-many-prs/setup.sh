#!/usr/bin/env bash
# Four open, green PRs — the shape the real lapse happened in.
#
# pr-links-on-merge-ask has no headroom: its old arm scores 4/4, because one
# PR whose URL the agent just fetched gets linked whatever the config says.
# A scenario where the baseline already passes cannot measure a rule that
# makes passing more likely.
#
# What actually went wrong was a long session with SEVEN PRs across two repos,
# whose numbers had been established many turns earlier, summarised in
# free-form prose with no fresh lookup. This stages the reachable part of
# that: several PRs at once, numbers supplied as prior context rather than
# discovered, and nothing to do but report.
source "$(dirname "$0")/../_lib/completion.sh" --prs \
  "acme/widget#1 OPEN feat/retry-backoff" \
  "acme/widget#2 OPEN feat/timeout-tuning" \
  "acme/widget#3 OPEN feat/log-sampling" \
  "acme/widget#4 OPEN feat/metric-names"
