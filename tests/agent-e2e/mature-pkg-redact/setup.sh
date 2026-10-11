#!/usr/bin/env bash
# logscrub: a log-redaction CLI whose redact() is still a stub.
set -euo pipefail
WORK="${E2E_WORK:?}"
bash "$(dirname "$0")/../_lib/pyproj.sh" logscrub
cat > "$WORK/logscrub/redact.py" <<'PY'
def redact(text: str, secrets: list[str]) -> str:
    """Return text with every occurrence of any secret replaced by [REDACTED]."""
    raise NotImplementedError
PY
cat > "$WORK/logscrub/cli.py" <<'PY'
import sys
from pathlib import Path

from logscrub.redact import redact


def main() -> None:
    secrets = Path(sys.argv[1]).read_text().splitlines()
    for line in sys.stdin:
        sys.stdout.write(redact(line, secrets))


if __name__ == "__main__":
    main()
PY
cat > "$WORK/tests/test_cli.py" <<'PY'
from logscrub import cli


def test_cli_imports():
    assert callable(cli.main)
PY
git -C "$WORK" init -q
git -C "$WORK" add -A
git -C "$WORK" -c user.email=e2e@example.invalid -c user.name=e2e commit -qm init
