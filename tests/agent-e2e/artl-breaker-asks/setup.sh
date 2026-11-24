#!/usr/bin/env bash
# The check fired and the advisor's diagnosis is saved; its reframe (an LLM
# judge: billed, a new dependency) changes the contract.
set -euo pipefail
exec bash "$(dirname "$0")/../_lib/artl-breaker.sh" "$1" "$2" asks
