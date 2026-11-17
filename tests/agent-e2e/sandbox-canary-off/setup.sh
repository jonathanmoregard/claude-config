#!/usr/bin/env bash
# Control for sandbox-canary: the same probe with the sandbox left as the
# arm ships it (disabled), showing every plant reads back and every workflow runs when
# the sandbox is off, so the enabled run's verdict is the sandbox's doing.
exec bash "$(dirname "$(readlink -f "$0")")/../sandbox-canary/setup.sh" "$1" "$2" 0
