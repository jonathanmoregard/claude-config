#!/usr/bin/env bash
# The control: round 2's findings share no cause. See _lib/artl-one-cause.sh.
set -euo pipefail
bash "$(dirname "$0")/../_lib/artl-one-cause.sh" unrelated
