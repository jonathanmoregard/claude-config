#!/usr/bin/env bash
# probe.sh records the cache dirs a sandboxed Bash call sees. With the
# SessionStart env hook they point into ~/.cache/claude-sandbox; check.sh
# grades the recorded file, not the agent's report.
set -euo pipefail
work=$1
run=$2
here=$(dirname "$(readlink -f "$0")")
{
  echo '#!/usr/bin/env bash'
  echo "printf 'sandbox=%s\nxdg=%s\ncargo=%s\n' \"\${SANDBOX_RUNTIME:-}\" \"\${XDG_CACHE_HOME:-}\" \"\${CARGO_HOME:-}\" | tee '$run/seen'"
} >"$work/probe.sh"
source "$here/../_lib/sandbox-on.sh"
