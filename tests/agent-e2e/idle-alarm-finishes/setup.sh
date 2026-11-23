#!/usr/bin/env bash
set -euo pipefail
bash "$(dirname "$0")/../_lib/idle-alarm.sh"
printf 'alpha\nbeta\ngamma\n' > "$E2E_WORK/names.txt"
