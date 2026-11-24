#!/usr/bin/env bash
# P3 (old arm): record which skill a sweep ask picks when assurance-sweep
# does not exist. The run stops at the first review skill it picks.
set -euo pipefail
L="$(dirname "$0")/../_lib"
bash "$L/docvault.sh" "${E2E_WORK:?}"
bash "$L/stop-at-review-skill.sh" "$E2E_WORK" "${E2E_RUN:?}"
