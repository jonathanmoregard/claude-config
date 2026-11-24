#!/usr/bin/env bash
# Round 2's reproduced findings are one cause, and the branch itself added
# the store the cause sits in. See _lib/artl-one-cause.sh.
set -euo pipefail
bash "$(dirname "$0")/../_lib/artl-one-cause.sh" fix
