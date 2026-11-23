"""Grade an agent's output with a model judge whose answer is schema-enforced.

Never read verdicts out of free text with regex or grep. The judge answers
through `claude -p --json-schema`, and the caller reads typed fields from
`structured_output`.

The judge's HOME and working directory are fresh and it runs with no tools,
settings, MCP or skills, so the config under test cannot shape its grade. It
authenticates with the access token alone (CLAUDE_CODE_OAUTH_TOKEN) read from
the real (passwd) home's credentials file: no credentials file and no refresh
token reach it, so it can never refresh, rotate or write the user's login. A
token too close to expiry is INCONCLUSIVE. An agent that spoils the real file
only makes the judge fail (INCONCLUSIVE), never pass.

The raw judge output, token redacted, goes to LOG_DIR/judge.out when the caller
passes a log_dir (a scenario passes its run dir, which the harness scrubs and
removes); without one nothing is kept.

usage (from a scenario's judge.py):
    from structured_judge import judge, Inconclusive
    verdict = judge(prompt, schema, log_dir=run)   # dict matching schema, or raises
"""
import json
import os
import pwd
import subprocess
import tempfile
import time

# Auth the judge must not inherit: it would override or bill around the token.
_AUTH_ENV = ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN')


class Inconclusive(Exception):
    """The judge did not produce a schema-valid answer."""


def _access_token(path: str, need_s: int) -> str:
    """The OAuth access token, valid for at least need_s more seconds."""
    try:
        with open(path, 'rb') as fh:
            oauth = json.load(fh)['claudeAiOauth']
        token, expires_ms = oauth['accessToken'], oauth['expiresAt']
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise Inconclusive(f'no usable credentials in {path} ({exc.__class__.__name__})') from exc
    if not isinstance(token, str) or not token or not isinstance(expires_ms, (int, float)):
        raise Inconclusive(f'no usable credentials in {path}')
    if expires_ms / 1000 < time.time() + need_s:
        raise Inconclusive('access token expires before the judge could finish; '
                           'rerun once Claude Code has refreshed it')
    return token


def judge(prompt: str, schema: dict, timeout: int = 600, log_dir: str | None = None) -> dict:
    raw = os.path.join(log_dir, 'judge.out') if log_dir else None
    see = f'; see {raw}' if raw else ''
    creds = os.environ.get('E2E_JUDGE_CREDS') or os.path.join(
        pwd.getpwuid(os.getuid()).pw_dir, '.claude', '.credentials.json')
    token = _access_token(creds, timeout)
    argv = [os.environ.get('E2E_JUDGE_CLAUDE', 'claude'), '-p',
            '--model', os.environ.get('E2E_JUDGE_MODEL', 'opus'),
            '--output-format', 'json', '--json-schema', json.dumps(schema),
            '--tools', '', '--strict-mcp-config', '--setting-sources', '',
            '--disable-slash-commands', '--no-session-persistence']
    try:
        with tempfile.TemporaryDirectory(prefix='e2e-judge-') as fresh:
            home, cwd = os.path.join(fresh, 'home'), os.path.join(fresh, 'cwd')
            os.makedirs(home)
            os.makedirs(cwd)
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith('CLAUDE') and k not in _AUTH_ENV}
            env.update(HOME=home, CLAUDE_CODE_OAUTH_TOKEN=token)
            # errors='replace': undecodable output becomes invalid JSON below,
            # i.e. Inconclusive, rather than an uncaught UnicodeDecodeError.
            proc = subprocess.run(argv, input=prompt, capture_output=True, text=True,
                                  errors='replace', env=env, cwd=cwd, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        _keep(raw, repr(exc))
        raise Inconclusive(f'{exc.__class__.__name__}{see}') from exc
    _keep(raw, (proc.stdout + proc.stderr).replace(token, '[REDACTED]'))
    if proc.returncode != 0:
        raise Inconclusive(f'judge exited {proc.returncode}{see}')
    try:
        envelope = json.loads(proc.stdout)
        if envelope.get('is_error') is not False:
            raise Inconclusive(f'judge reported an error{see}')
        verdict = envelope['structured_output']
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise Inconclusive(f'no structured_output ({exc.__class__.__name__}){see}') from exc
    if not isinstance(verdict, dict):
        raise Inconclusive(f'structured_output is not an object{see}')
    return verdict


def _keep(raw: str | None, text: str) -> None:
    """Write the raw log as a new private file. The log dir is a run dir the
    agent could write, so never follow or overwrite what is already there."""
    if raw is None:
        return
    try:
        fd = os.open(raw, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError:
        return  # the log is a convenience; the verdict does not depend on it
    with os.fdopen(fd, 'w', encoding='utf-8') as fh:
        fh.write(text)
