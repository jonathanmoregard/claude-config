#!/usr/bin/env bash
# The absolute-path route to the real gh. gh-path.txt names the unwrapped gh
# in /nix/store; $E2E_BIN/gh is a symlink to that same store binary, so plain
# `gh` reaches it too -- through bin/agent/gh on the new arm. The deny rule
# must refuse the store path typed into the Bash tool, while the wrapper, a
# child process, still execs the store binary (the rule binds the tool's
# command text, not the processes it starts).
set -euo pipefail
WORK="${E2E_WORK:?}"; RUN="${E2E_RUN:?}"; BIN="${E2E_BIN:?}"
store="$(readlink -f "$(command -v gh)")"
# The host's gh may be a credential wrapper that needs a token the sandbox
# lacks; take the unwrapped build it execs, which answers --version offline.
real="$(grep -aoE '/nix/store/[^ "]+-gh-[0-9][^ "/]*/bin/gh' "$store" | head -1 || true)"
[ -n "$real" ] || real="$store"
case "$real" in
  /nix/store/*/bin/gh) ;;
  *) echo "no /nix/store gh found: $real" >&2; exit 1 ;;
esac
"$real" --version >/dev/null
ln -s "$real" "$BIN/gh"
printf '%s\n' "$real" >"$WORK/gh-path.txt"
printf '%s\n' "$real" >"$RUN/store-gh"
cd "$WORK" && git init -q .
