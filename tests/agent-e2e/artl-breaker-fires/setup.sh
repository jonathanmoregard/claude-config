#!/usr/bin/env bash
# Ledger c=3,3, round 2 beside round 1 in the regex grader: the check fires.
set -euo pipefail
exec bash "$(dirname "$0")/../_lib/artl-breaker.sh" "$1" "$2" fires
