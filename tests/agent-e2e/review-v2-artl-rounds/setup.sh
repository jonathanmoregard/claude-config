#!/usr/bin/env bash
# P4: an ARTL close-out of the docvault diff (planted D7: a download link for
# one document opens any other), with a seeded ledger that one more empty D5
# run pushes over the drop threshold.
set -euo pipefail
bash "$(dirname "$0")/../_lib/docvault.sh" "${E2E_WORK:?}"
