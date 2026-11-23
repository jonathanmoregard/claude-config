#!/usr/bin/env bash
# Control: the same branch, but the ledger shows c=4,2 with round 2 in other
# files than round 1. The convergence check holds; round 3 goes ahead.
set -euo pipefail
bash "$(dirname "$0")/../_lib/artl-breaker.sh" "${E2E_WORK:?}" holds
