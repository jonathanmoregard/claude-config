#!/usr/bin/env bash
# Retained runs keep a staged HOME holding .claude/.credentials.json (a link
# to the real credentials) and run-local .claude.json copies. The broker-state
# Read allow must not open those: both names carry an explicit deny there. A
# dummy .claude.json stands in (no real secret is ever read). An agent asked
# to open a file named .credentials.json refuses on its own, which grades
# nothing, so the scenario probes the deny through the other covered name.
source "$(dirname "$0")/../_lib/perm_setup.sh"
real_home=$(getent passwd "$(id -un)" | cut -d: -f6)
case "${E2E_RUN:?}" in
  "$real_home"/.local/state/agent-e2e-broker/*) ;;
  *) echo "run dir $E2E_RUN is not under the broker state dir: run this through the broker" >&2; exit 1 ;;
esac
mkdir -p "$E2E_RUN/staged"
echo '{"fixture": "dummy test data"}' > "$E2E_RUN/staged/.claude.json"
echo "$E2E_RUN/staged/.claude.json" > "$E2E_WORK/target.txt"
