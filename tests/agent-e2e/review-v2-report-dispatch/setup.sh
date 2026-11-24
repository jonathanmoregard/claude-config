#!/usr/bin/env bash
# P1: the advisor answers a REPORT dispatch in prose in the requested shape.
set -euo pipefail
bash "$(dirname "$0")/../_lib/docvault.sh" "${E2E_WORK:?}"
