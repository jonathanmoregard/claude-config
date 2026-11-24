"""Grade an agent's output with a model judge whose answer is schema-enforced.

Never read verdicts out of free text with regex or grep. The judge answers
through `claude -p --json-schema`, and the caller reads typed fields from
`structured_output`.

Contract: judge() returns the schema-shaped verdict of a clean run (exit 0,
is_error false, an object in structured_output) or raises Inconclusive. Any
other failure, whatever its type, is Inconclusive too.

Isolation is of the judge's environment: a fresh HOME and working directory,
no tools, settings, MCP or slash commands, so the config under test cannot
change how the judge runs. The text being graded does reach the judge's
prompt; the schema is what keeps the answer a typed verdict.

Auth: the OAuth access token alone (CLAUDE_CODE_OAUTH_TOKEN), read from the
real (passwd) home's credentials file. No credentials file and no refresh
token reach the judge, so it can never refresh, rotate or write the user's
login; a token expiring within the timeout is Inconclusive. Inherited
ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN are dropped. Where there is no
credentials file at all (CI), an inherited ANTHROPIC_AUTH_TOKEN is used, as
the e2e harness itself does there.

Nothing is written outside a temp dir removed on return. On Inconclusive, the
message carries the tail of the judge's output with the token redacted;
check.sh prints it into the run's check log.

usage (from a scenario's judge.py):
    from structured_judge import judge, Inconclusive
    verdict = judge(prompt, schema)   # dict matching schema, or raises
"""
import json
import os
import pwd
import subprocess
import tempfile
import time

# Auth the judge must not inherit: it would override or bill around the token.
_AUTH_ENV = ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN')
_TAIL = 2000  # characters of judge output quoted in an Inconclusive message


class Inconclusive(Exception):
    """The judge did not produce a schema-valid answer."""


def judge(prompt: str, schema: dict, timeout: int = 600) -> dict:
    secret = None
    try:
        auth = _auth(timeout)
        secret = next(iter(auth.values()))
        return _run(prompt, schema, timeout, auth, secret)
    except Inconclusive:
        raise
    except Exception as exc:  # the contract: a verdict or Inconclusive, nothing else
        raise Inconclusive(_redact(f'{exc.__class__.__name__}: {exc}', secret)) from exc


def _auth(need_s: int) -> dict:
    """The one auth variable the judge gets, as {name: value}."""
    path = os.environ.get('E2E_JUDGE_CREDS') or os.path.join(
        pwd.getpwuid(os.getuid()).pw_dir, '.claude', '.credentials.json')
    try:
        with open(path, 'rb') as fh:
            raw = fh.read()
    except FileNotFoundError:
        ci = os.environ.get('ANTHROPIC_AUTH_TOKEN')
        if ci:
            return {'ANTHROPIC_AUTH_TOKEN': ci}
        raise Inconclusive(f'no credentials: {path} is missing and ANTHROPIC_AUTH_TOKEN is unset')
    try:
        oauth = json.loads(raw)['claudeAiOauth']
        token, expires_ms = oauth['accessToken'], oauth['expiresAt']
    except (ValueError, KeyError, TypeError, RecursionError) as exc:
        raise Inconclusive(f'no usable credentials in {path} ({exc.__class__.__name__})') from exc
    if not isinstance(token, str) or not token or not isinstance(expires_ms, (int, float)):
        raise Inconclusive(f'no usable credentials in {path}')
    if expires_ms / 1000 < time.time() + need_s:
        raise Inconclusive('access token expires before the judge could finish; '
                           'rerun once Claude Code has refreshed it')
    return {'CLAUDE_CODE_OAUTH_TOKEN': token}


def _run(prompt: str, schema: dict, timeout: int, auth: dict, secret: str) -> dict:
    argv = [os.environ.get('E2E_JUDGE_CLAUDE', 'claude'), '-p',
            '--model', os.environ.get('E2E_JUDGE_MODEL', 'opus'),
            '--output-format', 'json', '--json-schema', json.dumps(schema),
            '--tools', '', '--strict-mcp-config', '--setting-sources', '',
            '--disable-slash-commands', '--no-session-persistence']
    with tempfile.TemporaryDirectory(prefix='e2e-judge-') as fresh:
        home, cwd = os.path.join(fresh, 'home'), os.path.join(fresh, 'cwd')
        os.makedirs(home)
        os.makedirs(cwd)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith('CLAUDE') and k not in _AUTH_ENV}
        env.update(HOME=home, **auth)
        proc = subprocess.run(argv, input=prompt, capture_output=True, text=True,
                              env=env, cwd=cwd, timeout=timeout)
    tail = _redact(proc.stdout + proc.stderr, secret)[-_TAIL:]
    if proc.returncode != 0:
        raise Inconclusive(f'judge exited {proc.returncode}; output tail:\n{tail}')
    try:
        envelope = json.loads(proc.stdout)
        if envelope.get('is_error') is not False:
            raise Inconclusive(f'judge reported an error; output tail:\n{tail}')
        verdict = envelope['structured_output']
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise Inconclusive(f'no structured_output ({exc.__class__.__name__}); '
                           f'output tail:\n{tail}') from exc
    if not isinstance(verdict, dict):
        raise Inconclusive(f'structured_output is not an object; output tail:\n{tail}')
    return verdict


def _redact(text: str, secret: str | None) -> str:
    return text.replace(secret, '[REDACTED]') if secret else text
