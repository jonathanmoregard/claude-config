#!/usr/bin/env bash
# Control: round 2's findings share no cause.
set -euo pipefail
exec bash "$(dirname "$0")/../_lib/artl-one-cause.sh" "$1" "$2" unrelated
