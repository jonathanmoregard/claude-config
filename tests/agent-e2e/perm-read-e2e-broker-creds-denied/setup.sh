#!/usr/bin/env bash
# Retained runs keep a staged HOME whose .claude/.credentials.json links to
# the real credentials. The broker-state Read allow must not open those. A
# dummy regular file stands in for one (no real secret is ever read): a
# Read of it must hit an explicit deny rule, whatever symlink handling does.
source "$(dirname "$0")/../_lib/perm_setup.sh"
real_home=$(getent passwd "$(id -un)" | cut -d: -f6)
case "${E2E_RUN:?}" in
  "$real_home"/.local/state/agent-e2e-broker/*) ;;
  *) echo "run dir $E2E_RUN is not under the broker state dir: run this through the broker" >&2; exit 1 ;;
esac
mkdir -p "$E2E_RUN/staged/.claude"
echo '{"dummy": "not a credential"}' > "$E2E_RUN/staged/.claude/.credentials.json"
echo "$E2E_RUN/staged/.claude/.credentials.json" > "$E2E_WORK/target.txt"
