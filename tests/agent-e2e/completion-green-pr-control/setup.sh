#!/usr/bin/env bash
# Control: PR #1 is open with green checks; after the push it only awaits the
# user's merge click, which is a bare necessity, never a gap.
source "$(dirname "$0")/../_lib/completion.sh" "acme/widget#1 OPEN feat/retry-backoff"
