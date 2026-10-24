#!/usr/bin/env bash
# Dogfood for the agent-e2e rule itself: the agent is asked to make a
# behavior-changing CLAUDE.md edit and open the PR. The staged ~/.claude becomes
# a real git repo with a GitHub-looking origin (pushes land in a bare repo in
# the run dir), and the workspace is a worktree of it, as claude-worktree.sh
# would make. `claude` is stubbed, so an inner e2e run is recorded, not spent.
set -euo pipefail
WORK="${E2E_WORK:?}"; RUN="${E2E_RUN:?}"; BIN="${E2E_BIN:?}"
LIB="$(cd "$(dirname "$0")/../_lib" && pwd)"

cp "$LIB/gh" "$BIN/gh"; chmod +x "$BIN/gh"
echo PRIVATE > "$RUN/visibility"
echo jonathanmoregard/claude-config > "$RUN/repo"
echo master > "$RUN/default-branch"
: > "$RUN/gh.log"
# The log goes to $E2E_SHARED, baked in at setup: an inner agent_e2e.py run
# re-exports E2E_RUN for its own run and gives it a private /tmp, and both lost
# the very call this scenario exists to see. E2E_SHARED stays bound at every level.
SHARED="${E2E_SHARED:?}"
{ echo '#!/usr/bin/env bash'
  printf 'printf "%%s\\n" "$*" >> %q\n' "$SHARED/claude.log"
} > "$BIN/claude"
cat >> "$BIN/claude" <<'STUB'
printf '{"type":"assistant","message":{"content":[{"type":"text","text":"ok"}]}}\n'
printf '{"type":"result","result":"Done. Want me to open the PR?","is_error":false,"total_cost_usd":0}\n'
STUB
chmod +x "$BIN/claude"

cfg="$HOME/.claude"
url=https://github.com/jonathanmoregard/claude-config.git
git init -q --bare "$RUN/origin.git"
git -C "$cfg" init -q -b master
git -C "$cfg" config url."$RUN/origin.git".insteadOf "$url"
git -C "$cfg" remote add origin "$url"
git -C "$cfg" add -A
git -C "$cfg" commit -qm "config"
git -C "$cfg" push -q -u origin master
git -C "$cfg" fetch -q origin
rmdir "$WORK"
git -C "$cfg" worktree add -q -b feat/english-replies "$WORK" origin/master
