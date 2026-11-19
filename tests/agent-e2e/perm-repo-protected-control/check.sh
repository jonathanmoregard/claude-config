#!/usr/bin/env bash
# Repos edits are allowed per repo, not under a ~/Repos/** glob, so a checkout
# off that list keeps prompting. The target is aggregator, whose sync-agent.sh
# cron runs in place: an unreviewed edit there would execute unattended.
PERM_TOOLS=Write,Edit PERM_PATH=/home/jonathan/Repos/aggregator/E2E_SCRATCH.md PERM_EXPECT="prompted deny-rule"
source "$(dirname "$0")/../_lib/perm_scenario.sh" "$1"
