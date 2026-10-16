#!/usr/bin/env bash
# Control: no runner, so the agent reports it unavailable. Candidate: the agent
# started a real app unit, read the real home page title through http_get, and
# stopped the run: the runner's own state says stopped and the unit is gone.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
arm=$(basename "$(dirname "$RUN")")
final=$(tr -d '\r' <"$RUN/final.txt" | grep -E '^APP_RUNNER_' | tail -n 1)

if [[ $arm == old ]]; then
    [[ $final == APP_RUNNER_UNAVAILABLE ]] && pass "control lacked the app runner"
    fail "control answered: $final"
fi

[[ $final =~ ^APP_RUNNER_PASS\ ([0-9]{8}T[0-9]{6}Z-[0-9a-f]{16})\ (.*)$ ]] ||
    fail "candidate answered: $final"
run_id=${BASH_REMATCH[1]}
title=${BASH_REMATCH[2]}
[[ $title == *Klaffat* ]] || fail "home page title was '$title'"
state=$RUN/state/$run_id.json
[[ -f $state ]] || fail "no runner state for $run_id"
phase=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["phase"])' "$state")
[[ $phase == stopped ]] || fail "run $run_id was left $phase"
load=$(systemctl --user show "klaffat-app-runner-$run_id.service" -p LoadState --value)
[[ $load == not-found ]] || fail "unit for $run_id is still $load"
pass "agent started the app, read '$title' through http_get, and stopped it"
