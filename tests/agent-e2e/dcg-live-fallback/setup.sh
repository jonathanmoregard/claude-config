#!/usr/bin/env bash
# The state the banner exists for: dcg is loading nixos-config's bootstrap
# seed, not the operator's pattern list, and the marker agrees. The seed is the
# real file from nixos-config, loaded by the real dcg, so the alarm is raised
# off a live reading rather than off anything this scenario asserts.
set -euo pipefail
source "$(dirname "$0")/../_lib/dcg.sh"
make_plain_repo
seed_dcg_fallback
write_dcg_marker
