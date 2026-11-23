#!/usr/bin/env python3
"""SessionStart: give sandboxed Bash its own caches and git placeholder ignore.

Claude sources $CLAUDE_ENV_FILE before every Bash command, inside the sandbox
when the command is sandboxed. The block below acts only when SANDBOX_RUNTIME=1,
so unsandboxed commands, hooks and MCP servers keep the owner's caches: a
sandbox-writable cache the owner also reads would be a poisoning channel.
"""

import os
import sys

MARKER = "# claude-sandbox-env v1"

BLOCK = MARKER + r"""
if [ "${SANDBOX_RUNTIME:-}" = 1 ]; then
  export XDG_CACHE_HOME="$HOME/.cache/claude-sandbox"
  export CARGO_HOME="$HOME/.cache/claude-sandbox/cargo"
  export npm_config_cache="$HOME/.cache/claude-sandbox/npm"
  export npm_config_store_dir="$HOME/.cache/claude-sandbox/pnpm-store"
  if [ -f "$HOME/.config/git/ignore-claude-sandbox" ]; then
    _csb_n=${GIT_CONFIG_COUNT:-0}
    export "GIT_CONFIG_KEY_${_csb_n}=core.excludesFile"
    export "GIT_CONFIG_VALUE_${_csb_n}=$HOME/.config/git/ignore-claude-sandbox"
    export GIT_CONFIG_COUNT=$((_csb_n + 1))
    unset _csb_n
  fi
fi
"""


def main() -> int:
    # A sandbox write root must exist before the sandbox mounts it.
    os.makedirs(os.path.expanduser("~/.cache/claude-sandbox"), mode=0o700, exist_ok=True)
    path = os.environ.get("CLAUDE_ENV_FILE")
    if not path:
        return 0
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            if MARKER in f.read():
                return 0
    with open(path, "a", encoding="utf-8") as f:
        f.write(BLOCK)
    return 0


if __name__ == "__main__":
    sys.exit(main())
