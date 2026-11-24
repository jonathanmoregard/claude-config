#!/usr/bin/env bash
# P2: a diff-review ask picks advice-refine-test-loop, not assurance-sweep.
# The run stops at the first review skill it picks.
set -euo pipefail
L="$(dirname "$0")/../_lib"
bash "$L/docvault.sh" "${E2E_WORK:?}"
bash "$L/stop-at-review-skill.sh" "$E2E_WORK" "${E2E_RUN:?}"
