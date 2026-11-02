#!/usr/bin/env bash
# A marker left over from an earlier activation, and a dcg loading the tracked
# pattern list right now. The live reading and the snapshot disagree, and the
# live reading is the current one.
set -euo pipefail
source "$(dirname "$0")/../_lib/dcg.sh"
make_plain_repo
write_dcg_marker
# No seeding: dcg keeps the real config the harness copied in, so `dcg config`
# answers with the operator's own rule set. That is the whole setup -- the
# disagreement is between a stale file and a healthy loader.
