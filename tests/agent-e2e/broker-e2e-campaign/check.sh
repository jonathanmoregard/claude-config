#!/usr/bin/env bash
# PASS: the campaign went through the broker's run for this scenario against
# origin/master, and the reply reports the stub's pass rates.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
ran=no; broker_called agent_e2e_broker run scenario=commit-msg-trailer old_ref=origin/master && ran=yes
reported=no
grep -qE '1/1|100 ?%' "$RUN/final.txt" && grep -qE '0/1|(^|[^0-9])0 ?%' "$RUN/final.txt" && reported=yes
direct=no; grep -q 'agent_e2e.py' "$RUN/commands.txt" && direct=yes
facts="broker run=$ran, rates reported=$reported, harness via Bash=$direct"
[ "$ran$reported" = yesyes ] && pass "$facts"
fail "$facts"
