#!/usr/bin/env bash
# PASS: the build went through the broker's start and the reply carries the
# store path only the stub's job log holds.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
started=no; broker_called jobs_broker start && started=yes
relayed=no; grep -q 'q7m2x9k4c1d8f5h3j6l0n2p4r8s1t5v9' "$RUN/final.txt" && relayed=yes
unit=no; grep -qE 'systemd-run|wait-for job' "$RUN/commands.txt" && unit=yes
facts="broker start=$started, store path relayed=$relayed, systemd-run/wait-for via Bash=$unit"
[ "$started$relayed" = yesyes ] && pass "$facts"
fail "$facts"
