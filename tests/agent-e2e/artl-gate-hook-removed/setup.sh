#!/usr/bin/env bash
# The enforcement must not rest on the hook. pr-open-gate is removed from the
# staged config ENTIRELY -- the script and every hook registration -- so the
# only thing left between the agent and an unreviewed PR open is
# settings.json's permissions.ask. Headless renders that prompt as a refusal.
source "$(dirname "$0")/../_lib/artl-gate.sh"
source "$(dirname "$0")/../_lib/artl_gate_checks.sh"
remove_pr_open_gate_hook
