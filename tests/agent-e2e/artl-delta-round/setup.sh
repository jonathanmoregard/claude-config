#!/usr/bin/env bash
# A T2 diff, resumed after round 1: adjudicated, and its fix committed.
set -euo pipefail
exec bash "$(dirname "$0")/../_lib/artl-tiers.sh" "$1" "$2" delta
