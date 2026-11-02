#!/usr/bin/env bash
# The state the recovery steps are written for, staged coherently: a projected
# host, a healthy projection serving the real rule list, and the bootstrap seed
# sitting at the link path where activation left it. This is the one state in
# which moving that file aside is the correct action, so it is the only one
# that can show whether what reaches the operator is safe AND usable.
set -euo pipefail
source "$(dirname "$0")/../_lib/dcg.sh"
make_plain_repo
seed_dcg_fallback
write_dcg_marker
mark_projected_host
stage_seed_collision
