#!/usr/bin/env bash
# Every retained run keeps its staged HOME under the broker state dir:
# .claude/.credentials.json (a link to the real credentials, or the mount point
# bwrap bound them on), .claude.json and
# .claude/backups/.claude.json.backup.* copies. The broker-state Read allow
# must not open any of it, so every staged home/ is denied wholesale. A dummy
# backup file at the same relative path stands in (no real secret is read),
# under a neutral name an agent will agree to open.
source "$(dirname "$0")/../_lib/perm_setup.sh"
real_home=$(getent passwd "$(id -un)" | cut -d: -f6)
case "${E2E_RUN:?}" in
  "$real_home"/.local/state/agent-e2e-broker/*) ;;
  *) echo "run dir $E2E_RUN is not under the broker state dir: run this through the broker" >&2; exit 1 ;;
esac
mkdir -p "$E2E_RUN/fixture/home/.claude/backups"
echo '{"fixture": "dummy test data"}' > "$E2E_RUN/fixture/home/.claude/backups/fixture.backup.json"
echo "$E2E_RUN/fixture/home/.claude/backups/fixture.backup.json" > "$E2E_WORK/target.txt"
