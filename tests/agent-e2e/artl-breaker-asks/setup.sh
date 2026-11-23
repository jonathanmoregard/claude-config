#!/usr/bin/env bash
# The breaker already fired and the advisor's diagnosis is saved: the reframe
# it proposes changes the contract (an LLM judge, billed, a new dependency).
set -euo pipefail
bash "$(dirname "$0")/../_lib/artl-breaker.sh" "${E2E_WORK:?}" asks
