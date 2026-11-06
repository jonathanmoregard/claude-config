#!/usr/bin/env bash
# Control for sandbox-write-roots: the same probe with the sandbox left as the
# arm ships it (disabled), showing the declared roots enforce nothing until
# it is enabled and that the outside target is one the harness allows.
SANDBOX_ENABLE=0 exec bash "$(dirname "$(readlink -f "$0")")/../sandbox-write-roots/setup.sh" "$@"
