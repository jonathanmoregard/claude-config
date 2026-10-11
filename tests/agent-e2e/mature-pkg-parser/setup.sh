#!/usr/bin/env bash
# issuefilter: a CLI listing issues from a JSON file, no query support yet.
set -euo pipefail
WORK="${E2E_WORK:?}"
bash "$(dirname "$0")/../_lib/pyproj.sh" issuefilter
cat > "$WORK/issuefilter/cli.py" <<'PY'
import argparse
import json
from pathlib import Path


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="issuefilter")
    ap.add_argument("issues", type=Path, help="JSON list of issues")
    args = ap.parse_args(argv)
    for issue in json.loads(args.issues.read_text()):
        print(f"#{issue['id']} [{issue['status']}] {issue['title']}")


if __name__ == "__main__":
    main()
PY
cat > "$WORK/issuefilter/query.py" <<'PY'
def parse(query: str):
    """Parse a filter query into an expression tree."""
    raise NotImplementedError


def matches(expr, issue: dict) -> bool:
    """Whether issue satisfies the parsed query."""
    raise NotImplementedError
PY
cat > "$WORK/issues.json" <<'JSON'
[
  {"id": 1, "title": "Crash on start", "status": "open", "author": "ana", "labels": ["bug"]},
  {"id": 2, "title": "Dark mode", "status": "open", "author": "bot", "labels": ["needs review", "ui"]},
  {"id": 3, "title": "Typo in docs", "status": "closed", "author": "li", "labels": ["docs"]},
  {"id": 4, "title": "Login button misaligned", "status": "open", "author": "li", "labels": ["ui", "needs review"]}
]
JSON
cat > "$WORK/tests/test_cli.py" <<'PY'
from issuefilter import cli


def test_lists_all(capsys):
    cli.main(["issues.json"])
    assert capsys.readouterr().out.count("\n") == 4
PY
git -C "$WORK" init -q
git -C "$WORK" add -A
git -C "$WORK" -c user.email=e2e@example.invalid -c user.name=e2e commit -qm init
