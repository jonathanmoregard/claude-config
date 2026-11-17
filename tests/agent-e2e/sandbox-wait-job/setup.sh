#!/usr/bin/env bash
# wait-job-main with the Bash sandbox on: no user bus, so no transient unit.
set -euo pipefail
here=$(dirname "$(readlink -f "$0")")
WAITJOB_STEPS=2 bash "$here/../_lib/waitjob.sh"
source "$here/../_lib/sandbox-on.sh"
