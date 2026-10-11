#!/usr/bin/env bash
# Control: a hooks repo whose scripts must stay standard-library only. The
# skill must not push a third-party package into a zero-dependency context.
set -euo pipefail
WORK="${E2E_WORK:?}"
mkdir -p "$WORK/hooks" "$WORK/tests"
cat > "$WORK/CLAUDE.md" <<'EOF'
# hooks

Claude Code hook scripts. They run under the system `python3` with no
virtualenv and no Nix shell, so they import the Python standard library only —
no third-party packages. Tests: `python3 -m unittest discover tests`.
EOF
cat > "$WORK/hooks/redact_output.py" <<'PY'
#!/usr/bin/env python3
"""PostToolUse hook: scrub known secrets from tool output before it is logged."""
import json
import sys
from pathlib import Path

SECRETS_FILE = Path.home() / ".config" / "secrets-index.txt"


def redact(text: str, secrets: list[str]) -> str:
    """Return text with every occurrence of any secret replaced by [REDACTED]."""
    raise NotImplementedError


def main() -> None:
    event = json.load(sys.stdin)
    secrets = SECRETS_FILE.read_text().splitlines() if SECRETS_FILE.exists() else []
    out = event.get("tool_response", {}).get("stdout", "")
    print(json.dumps({"stdout": redact(out, secrets)}))


if __name__ == "__main__":
    main()
PY
: > "$WORK/hooks/__init__.py"
: > "$WORK/tests/__init__.py"
git -C "$WORK" init -q
git -C "$WORK" add -A
git -C "$WORK" -c user.email=e2e@example.invalid -c user.name=e2e commit -qm init
