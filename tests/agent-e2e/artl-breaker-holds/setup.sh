#!/usr/bin/env bash
# Control: ledger c=4,2, round 2 in other files than round 1: the check holds.
set -euo pipefail
exec bash "$(dirname "$0")/../_lib/artl-breaker.sh" "$1" "$2" holds
