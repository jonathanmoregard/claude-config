#!/usr/bin/env bash
set -euo pipefail
WAITJOB_START=1 WAITJOB_STEPS=9 bash "$(dirname "$0")/../_lib/waitjob.sh"
