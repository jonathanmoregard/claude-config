#!/usr/bin/env bash
# Round 2's findings share one cause, in a store the diff does not touch.
set -euo pipefail
exec bash "$(dirname "$0")/../_lib/artl-one-cause.sh" "$1" "$2" ask
