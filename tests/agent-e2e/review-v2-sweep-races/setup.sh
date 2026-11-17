#!/usr/bin/env bash
# P3 (new arm): a Races-only assurance sweep on a repo with two writers to one
# row and no way to run two operations at once in a test.
set -euo pipefail
bash "$(dirname "$0")/../_lib/docvault.sh" "${E2E_WORK:?}"
