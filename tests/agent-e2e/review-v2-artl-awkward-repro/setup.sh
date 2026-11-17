#!/usr/bin/env bash
# P5: the planted scope check refuses a link presented for ANOTHER owner's
# document (the obvious reproducer passes) but opens every other document of
# the SAME owner. The finding must end reproduced or unconfirmed, never
# refuted without a cited file and line.
set -euo pipefail
bash "$(dirname "$0")/../_lib/docvault.sh" "${E2E_WORK:?}" owner-and
