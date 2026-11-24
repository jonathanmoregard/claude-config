#!/usr/bin/env bash
# A T2 diff, resumed after round 3: a third reproduced defect in app/quota.py,
# this one in the line round 2's fix wrote.
set -euo pipefail
exec bash "$(dirname "$0")/../_lib/artl-tiers.sh" "$1" "$2" round3
