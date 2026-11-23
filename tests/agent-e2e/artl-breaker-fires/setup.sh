#!/usr/bin/env bash
# A close-out after round 2 whose ledger shows c=3,3, round 2 landing within
# a few lines of round 1 in the regex grader: the convergence check fires.
set -euo pipefail
bash "$(dirname "$0")/../_lib/artl-breaker.sh" "${E2E_WORK:?}" fires
