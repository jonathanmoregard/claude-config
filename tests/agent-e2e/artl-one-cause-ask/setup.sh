#!/usr/bin/env bash
# Round 2's reproduced findings are one cause, and the cause sits in a store
# the diff does not touch. See _lib/artl-one-cause.sh.
set -euo pipefail
bash "$(dirname "$0")/../_lib/artl-one-cause.sh" ask
