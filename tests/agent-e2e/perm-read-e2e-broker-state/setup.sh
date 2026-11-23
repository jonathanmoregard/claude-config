#!/usr/bin/env bash
# A reviewer reading a campaign's transcripts: the broker keeps them under
# ~/.local/state/agent-e2e-broker/campaigns/, outside every working directory.
# The run dir of this very campaign lives there when the broker runs it, so a
# probe file written into it is a real file at the path the rule names.
source "$(dirname "$0")/../_lib/perm_setup.sh"
case "${E2E_RUN:?}" in
  "$HOME"/.local/state/agent-e2e-broker/*) ;;
  *) echo "run dir $E2E_RUN is not under the broker state dir: run this through the broker" >&2; exit 1 ;;
esac
echo "probe: campaign transcript stand-in" > "$E2E_RUN/probe.txt"
echo "$E2E_RUN/probe.txt" > "$E2E_WORK/target.txt"
