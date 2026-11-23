#!/usr/bin/env python3
"""tests/agent-e2e/_lib/structured_judge.py returns a verdict only from a
successful, schema-shaped judge result: a failed exit, an error result or a
missing structured_output is Inconclusive, even when a verdict rides along.
The judge CLI is a fake (E2E_JUDGE_CLAUDE); no model runs."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import sys
import tempfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "agent-e2e" / "_lib"))
from structured_judge import Inconclusive, judge  # noqa: E402

VERDICT = '"structured_output":{"ok":true}'
SCHEMA = {"type": "object"}


def fake(tmp: Path, stdout: str, code: int) -> str:
    path = tmp / "claude"
    path.write_text(f"#!/bin/sh\ncat >/dev/null\nprintf '%s\\n' '{stdout}'\nexit {code}\n",
                    encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


@pytest.fixture
def env(monkeypatch, tmp_path):
    creds = tmp_path / "creds.json"
    creds.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("E2E_JUDGE_CREDS", str(creds))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    tempfile.tempdir = None
    yield tmp_path
    tempfile.tempdir = None


def test_success_returns_the_structured_output(env, monkeypatch) -> None:
    monkeypatch.setenv("E2E_JUDGE_CLAUDE", fake(env, '{"is_error":false,' + VERDICT + "}", 0))
    assert judge("p", SCHEMA) == {"ok": True}


@pytest.mark.parametrize("stdout,code", [
    ('{"is_error":true,' + VERDICT + "}", 1),   # failed exit
    ('{"is_error":true,' + VERDICT + "}", 0),   # error result, exit 0
    ('{' + VERDICT + "}", 0),                   # no is_error field
    ('{"is_error":false,"result":"ok"}', 0),    # no structured_output
    ("not json", 0),
])
def test_anything_but_a_clean_result_is_inconclusive(env, monkeypatch, stdout, code) -> None:
    monkeypatch.setenv("E2E_JUDGE_CLAUDE", fake(env, stdout, code))
    with pytest.raises(Inconclusive):
        judge("p", SCHEMA)


def test_credentials_are_copied_private_never_linked(env, monkeypatch) -> None:
    seen = env / "seen"
    script = env / "claude"
    script.write_text(
        "#!/bin/sh\ncat >/dev/null\n"
        f'f="$HOME/.claude/.credentials.json"; [ -L "$f" ] && echo link > {seen} '
        f'|| stat -c %a "$f" > {seen}\n'
        "printf '%s\\n' '{\"is_error\":false," + VERDICT + "}'\n", encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("E2E_JUDGE_CLAUDE", str(script))
    judge("p", SCHEMA)
    assert seen.read_text().strip() == "600"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
