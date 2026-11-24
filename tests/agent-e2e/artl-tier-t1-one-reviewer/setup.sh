#!/usr/bin/env bash
# A T1 diff (the skill's tier table): the branch adds a daily reset to the quota.
set -euo pipefail
exec bash "$(dirname "$0")/../_lib/artl-tiers.sh" "$1" "$2" t1
