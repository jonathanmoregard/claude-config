#!/usr/bin/env python3
"""tests/agent-e2e/_lib/structured_judge.py returns a verdict only from a
successful, schema-shaped judge result: a failed exit, an error result or a
missing structured_output is Inconclusive, even when a verdict rides along.
The judge CLI is a fake (E2E_JUDGE_CLAUDE); no model runs."""
from __future__ import annotations

import json
from pathlib import Path
import stat
import sys
import tempfile
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "agent-e2e" / "_lib"))
from structured_judge import Inconclusive, judge  # noqa: E402

VERDICT = '"structured_output":{"ok":true}'
SCHEMA = {"type": "object"}
TOKEN = "e2e-fake-access-token"
FUTURE_MS = int(time.time() * 1000) + 24 * 3600 * 1000


def fake(tmp: Path, stdout: str, code: int) -> str:
    path = tmp / "claude"
    path.write_text(f"#!/bin/sh\ncat >/dev/null\nprintf '%s\\n' '{stdout}'\nexit {code}\n",
                    encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


@pytest.fixture
def env(monkeypatch, tmp_path):
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": TOKEN, "refreshToken": "e2e-fake-refresh", "expiresAt": FUTURE_MS}}),
        encoding="utf-8")
    monkeypatch.setenv("E2E_JUDGE_CREDS", str(creds))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    tempfile.tempdir = None
    yield tmp_path
    tempfile.tempdir = None


def test_success_returns_the_structured_output(env, monkeypatch) -> None:
    monkeypatch.setenv("E2E_JUDGE_CLAUDE", fake(env, '{"is_error":false,' + VERDICT + "}", 0))
    assert judge("p", SCHEMA) == {"ok": True}


@pytest.mark.parametrize("stdout,code", [
    ('{"is_error":false,' + VERDICT + "}", 1),  # failed exit, clean-looking body
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


def script(tmp: Path, body: str) -> str:
    path = tmp / "claude"
    path.write_text("#!/bin/sh\ncat >/dev/null\n" + body
                    + "printf '%s\\n' '{\"is_error\":false," + VERDICT + "}'\n",
                    encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def test_judge_gets_the_access_token_only_and_no_credentials_file(env, monkeypatch) -> None:
    seen = env / "seen"
    before = (env / "creds.json").read_bytes()
    monkeypatch.setenv("E2E_JUDGE_CLAUDE", script(env, (
        f'f="$HOME/.claude/.credentials.json"; [ -e "$f" ] || [ -L "$f" ] && echo file > {seen}\n'
        f'echo "token=$CLAUDE_CODE_OAUTH_TOKEN" >> {seen}\n'
        # A refresh would write here; it must not reach the real file.
        'mkdir -p "$HOME/.claude"; echo rotated > "$f"\n')))
    assert judge("p", SCHEMA) == {"ok": True}
    assert seen.read_text() == f"token={TOKEN}\n"
    assert (env / "creds.json").read_bytes() == before


def test_inherited_claude_auth_is_not_passed_through(env, monkeypatch) -> None:
    seen = env / "seen"
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "inherited")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "billed-key")
    monkeypatch.setenv("E2E_JUDGE_CLAUDE", script(
        env, f'echo "$CLAUDE_CODE_OAUTH_TOKEN|${{ANTHROPIC_API_KEY-unset}}" > {seen}\n'))
    judge("p", SCHEMA)
    assert seen.read_text() == f"{TOKEN}|unset\n"


@pytest.mark.parametrize("creds", [
    {"claudeAiOauth": {"accessToken": TOKEN, "expiresAt": 1000}},     # expired
    {"claudeAiOauth": {"accessToken": TOKEN,
                       "expiresAt": int(time.time() * 1000) + 60_000}},  # expires mid-run
    {"claudeAiOauth": {"expiresAt": FUTURE_MS}},                      # no token
    {"claudeAiOauth": {"accessToken": "", "expiresAt": FUTURE_MS}},   # empty token
    {"other": 1},
    "not json",
])
def test_unusable_credentials_are_inconclusive_before_the_judge_runs(env, monkeypatch, creds) -> None:
    path = env / "creds.json"
    path.write_text(creds if isinstance(creds, str) else json.dumps(creds), encoding="utf-8")
    before = path.read_bytes()
    ran = env / "ran"
    monkeypatch.setenv("E2E_JUDGE_CLAUDE", script(env, f"touch {ran}\n"))
    with pytest.raises(Inconclusive):
        judge("p", SCHEMA)
    assert not ran.exists()
    assert path.read_bytes() == before


def test_token_never_reaches_the_raw_output_file(env, monkeypatch) -> None:
    monkeypatch.setenv("E2E_JUDGE_CLAUDE", script(
        env, 'echo "out $CLAUDE_CODE_OAUTH_TOKEN"; echo "err $CLAUDE_CODE_OAUTH_TOKEN" >&2\n'))
    with pytest.raises(Inconclusive) as exc:  # the echo spoils the JSON stdout
        judge("p", SCHEMA)
    raw = Path(str(exc.value).rsplit("see ", 1)[1]).read_text()
    assert "err [REDACTED]" in raw
    assert TOKEN not in raw


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
