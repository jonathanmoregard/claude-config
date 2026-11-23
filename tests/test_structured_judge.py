#!/usr/bin/env python3
"""tests/agent-e2e/_lib/structured_judge.py returns a verdict only from a
successful, schema-shaped judge result: a failed exit, an error result or a
missing or non-object structured_output is Inconclusive, even when a verdict
rides along. The judge runs isolated (fresh HOME and cwd, no tools, settings,
MCP or slash commands) and gets the OAuth access token alone: no credentials
file, no refresh token, no inherited API auth. The judge CLI is a fake
(E2E_JUDGE_CLAUDE); no model runs."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "agent-e2e" / "_lib"))
from structured_judge import Inconclusive, judge  # noqa: E402

VERDICT = '"structured_output":{"ok":true}'
CLEAN = '{"is_error":false,' + VERDICT + '}'
SCHEMA = {"type": "object"}
TOKEN = "e2e-fake-access-token"
REFRESH = "e2e-fake-refresh-token"
FUTURE_MS = int(time.time() * 1000) + 24 * 3600 * 1000


def script(tmp: Path, body: str = "", stdout: str = CLEAN, code: int = 0) -> str:
    path = tmp / "claude"
    path.write_text(f"#!/bin/sh\ncat >/dev/null\n{body}printf '%s\\n' '{stdout}'\nexit {code}\n",
                    encoding="utf-8")
    path.chmod(0o755)
    return str(path)


@pytest.fixture
def env(monkeypatch, tmp_path):
    """The caller's HOME holds the credentials, so a judge that stopped
    overriding HOME would write the very file the tests check."""
    home = tmp_path / "caller-home"
    (home / ".claude").mkdir(parents=True)
    creds = home / ".claude" / ".credentials.json"
    creds.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": TOKEN, "refreshToken": REFRESH, "expiresAt": FUTURE_MS}}),
        encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("E2E_JUDGE_CREDS", str(creds))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    tempfile.tempdir = None
    yield tmp_path
    tempfile.tempdir = None


def creds_of(tmp: Path) -> Path:
    return tmp / "caller-home" / ".claude" / ".credentials.json"


def test_success_returns_the_structured_output(env, monkeypatch) -> None:
    monkeypatch.setenv("E2E_JUDGE_CLAUDE", script(env))
    assert judge("p", SCHEMA) == {"ok": True}


@pytest.mark.parametrize("stdout,code", [
    (CLEAN, 1),                                                 # failed exit, clean body
    ('{"is_error":true,' + VERDICT + "}", 0),                   # error result, exit 0
    ('{' + VERDICT + "}", 0),                                   # no is_error field
    ('{"is_error":false,"result":"ok"}', 0),                    # no structured_output
    ('{"is_error":false,"structured_output":null}', 0),         # null verdict
    ('{"is_error":false,"structured_output":"ok"}', 0),         # string verdict
    ("not json", 0),
])
def test_anything_but_a_clean_result_is_inconclusive(env, monkeypatch, stdout, code) -> None:
    monkeypatch.setenv("E2E_JUDGE_CLAUDE", script(env, stdout=stdout, code=code))
    with pytest.raises(Inconclusive):
        judge("p", SCHEMA)


def test_undecodable_output_is_inconclusive(env, monkeypatch) -> None:
    path = env / "claude"
    path.write_text("#!/bin/sh\ncat >/dev/null\nprintf '\\377'\n", encoding="utf-8")
    path.chmod(0o755)
    monkeypatch.setenv("E2E_JUDGE_CLAUDE", str(path))
    with pytest.raises(Inconclusive):
        judge("p", SCHEMA)


def test_judge_runs_isolated(env, monkeypatch) -> None:
    monkeypatch.setenv("E2E_JUDGE_CLAUDE", script(env, (
        f'pwd > {env}/cwd.txt; ls -A > {env}/ls.txt; printf %s "$HOME" > {env}/home.txt\n'
        f'for a in "$@"; do printf "<%s>" "$a"; done > {env}/args.txt\n')))
    judge("p", SCHEMA)
    cwd = Path((env / "cwd.txt").read_text().strip())
    home = Path((env / "home.txt").read_text())
    args = (env / "args.txt").read_text()
    assert cwd.name == "cwd" and cwd.parent == home.parent and cwd.parent.parent == env
    assert home.name == "home"
    assert (env / "ls.txt").read_text() == ""
    for flags in ("<--tools><>", "<--strict-mcp-config>", "<--setting-sources><>",
                  "<--disable-slash-commands>", "<--no-session-persistence>",
                  "<--output-format><json>", "<--json-schema><" + json.dumps(SCHEMA) + ">"):
        assert flags in args, flags


def test_judge_gets_the_access_token_only(env, monkeypatch) -> None:
    seen = env / "seen"
    before = creds_of(env).read_bytes()
    monkeypatch.setenv("E2E_JUDGE_CLAUDE", script(env, (
        f'f="$HOME/.claude/.credentials.json"; {{ [ -e "$f" ] || [ -L "$f" ]; }} && touch {env}/had-file\n'
        f'{{ env; printf "%s\\n" "$@"; }} > {seen}\n'
        # A refresh would write here; it must not reach the caller's file.
        'mkdir -p "$HOME/.claude"; echo rotated > "$f"\n')))
    assert judge("p", SCHEMA) == {"ok": True}
    dump = seen.read_text()
    assert not (env / "had-file").exists()
    assert f"CLAUDE_CODE_OAUTH_TOKEN={TOKEN}\n" in dump
    assert REFRESH not in dump
    assert creds_of(env).read_bytes() == before


def test_inherited_auth_is_not_passed_through(env, monkeypatch) -> None:
    seen = env / "seen"
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "inherited")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "billed-key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "other-token")
    monkeypatch.setenv("E2E_JUDGE_CLAUDE", script(env, (
        f'echo "$CLAUDE_CODE_OAUTH_TOKEN|${{ANTHROPIC_API_KEY-unset}}|'
        f'${{ANTHROPIC_AUTH_TOKEN-unset}}" > {seen}\n')))
    judge("p", SCHEMA)
    assert seen.read_text() == f"{TOKEN}|unset|unset\n"


@pytest.mark.parametrize("creds", [
    {"claudeAiOauth": {"accessToken": TOKEN, "expiresAt": 1000}},     # expired
    {"claudeAiOauth": {"accessToken": TOKEN,
                       "expiresAt": int(time.time() * 1000) + 60_000}},  # expires mid-run
    {"claudeAiOauth": {"expiresAt": FUTURE_MS}},                      # no token
    {"claudeAiOauth": {"accessToken": "", "expiresAt": FUTURE_MS}},   # empty token
    {"claudeAiOauth": {"accessToken": 7, "expiresAt": FUTURE_MS}},    # non-string token
    {"claudeAiOauth": {"accessToken": TOKEN, "expiresAt": "soon"}},   # non-numeric expiry
    {"other": 1},
    "not json",
    None,                                                             # no file at all
])
def test_unusable_credentials_are_inconclusive_before_the_judge_runs(env, monkeypatch, creds) -> None:
    path = creds_of(env)
    if creds is None:
        path.unlink()
    else:
        path.write_text(creds if isinstance(creds, str) else json.dumps(creds), encoding="utf-8")
    before = path.read_bytes() if path.exists() else None
    ran = env / "ran"
    monkeypatch.setenv("E2E_JUDGE_CLAUDE", script(env, f"touch {ran}\n"))
    with pytest.raises(Inconclusive):
        judge("p", SCHEMA)
    assert not ran.exists()
    assert (path.read_bytes() if path.exists() else None) == before


def test_failure_message_quotes_the_output_with_the_token_redacted(env, monkeypatch) -> None:
    monkeypatch.setenv("E2E_JUDGE_CLAUDE", script(
        env, 'echo "err $CLAUDE_CODE_OAUTH_TOKEN" >&2\n', code=1))
    with pytest.raises(Inconclusive) as exc:
        judge("p", SCHEMA)
    assert "err [REDACTED]" in str(exc.value)
    assert TOKEN not in str(exc.value)


@pytest.mark.parametrize("code", [0, 1])
def test_nothing_is_left_behind(env, monkeypatch, code) -> None:
    monkeypatch.setenv("E2E_JUDGE_CLAUDE", script(env, code=code))
    before = set(os.listdir(env))
    try:
        judge("p", SCHEMA)
    except Inconclusive:
        pass
    assert set(os.listdir(env)) - {"claude"} == before - {"claude"}


def test_any_failure_type_is_inconclusive(env, monkeypatch) -> None:
    monkeypatch.setenv("E2E_JUDGE_CLAUDE", str(env / "no-such-cli"))   # OSError
    with pytest.raises(Inconclusive):
        judge("p", SCHEMA)
    monkeypatch.setenv("E2E_JUDGE_CLAUDE", script(env, "sleep 5\n"))   # TimeoutExpired
    with pytest.raises(Inconclusive):
        judge("p", SCHEMA, timeout=1)
    monkeypatch.setenv("E2E_JUDGE_CLAUDE", script(env))
    monkeypatch.setattr("subprocess.run", lambda *a, **k: 1 / 0)       # anything else
    with pytest.raises(Inconclusive) as exc:
        judge("p", SCHEMA)
    assert "ZeroDivisionError" in str(exc.value)


@pytest.mark.parametrize("raw", [
    '{"claudeAiOauth":{"accessToken":"a\\u0000b","expiresAt":%d}}' % FUTURE_MS,  # NUL in token
    "[" * 100000,                                                                # deep nesting
])
def test_hostile_credentials_are_inconclusive(env, monkeypatch, raw) -> None:
    creds_of(env).write_text(raw, encoding="utf-8")
    monkeypatch.setenv("E2E_JUDGE_CLAUDE", script(env))
    with pytest.raises(Inconclusive):
        judge("p", SCHEMA)


def test_inherited_claude_settings_are_not_passed_through(env, monkeypatch) -> None:
    seen = env / "seen"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(env / "agent-config"))
    monkeypatch.setenv("E2E_JUDGE_CLAUDE", script(
        env, f'echo "${{CLAUDE_CONFIG_DIR-unset}}" > {seen}\n'))
    judge("p", SCHEMA)
    assert seen.read_text() == "unset\n"


def test_without_a_credentials_file_ci_auth_is_used_and_redacted(env, monkeypatch) -> None:
    creds_of(env).unlink()
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "ci-token")
    monkeypatch.setenv("E2E_JUDGE_CLAUDE", script(env, (
        'echo "${ANTHROPIC_AUTH_TOKEN-unset}|${CLAUDE_CODE_OAUTH_TOKEN-unset}" >&2\n'), code=1))
    with pytest.raises(Inconclusive) as exc:
        judge("p", SCHEMA)
    assert "[REDACTED]|unset" in str(exc.value)
    assert "ci-token" not in str(exc.value)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
