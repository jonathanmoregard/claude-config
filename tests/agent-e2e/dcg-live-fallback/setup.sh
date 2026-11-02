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
# This host is a projected one, so the FIX line's projected branch is the one
# an operator here would have to run. Without this the sandbox reads as a flat
# ~/.claude checkout and the agent relays the other branch -- correctly for
# what it was shown, uselessly for where this config runs.
mark_projected_host
