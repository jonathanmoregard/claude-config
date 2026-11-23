"""Grade an agent's output with a model judge whose answer is schema-enforced.

Never read verdicts out of free text with regex or grep. The judge answers
through `claude -p --json-schema`, and the caller reads typed fields from
`structured_output`.

The judge's HOME and working directory are fresh and it runs with no tools,
settings, MCP or skills, so the config under test cannot shape its grade. Its
credentials are a private 0600 copy of the real (passwd) home's file, removed
with the temp dir; nothing the judge does writes to the real file. An agent
that spoils the real file only makes the judge fail (INCONCLUSIVE), never pass.

usage (from a scenario's judge.py):
    from structured_judge import judge, Inconclusive
    verdict = judge(prompt, schema)   # dict matching schema, or raises
"""
import json
import os
import pwd
import subprocess
import tempfile


class Inconclusive(Exception):
    """The judge did not produce a schema-valid answer."""


def judge(prompt: str, schema: dict, timeout: int = 600) -> dict:
    out_dir = tempfile.mkdtemp(prefix='e2e-judge-out-')
    raw = os.path.join(out_dir, 'judge.out')
    creds = os.path.join(pwd.getpwuid(os.getuid()).pw_dir, '.claude', '.credentials.json')
    argv = [os.environ.get('E2E_JUDGE_CLAUDE', 'claude'), '-p',
            '--model', os.environ.get('E2E_JUDGE_MODEL', 'opus'),
            '--output-format', 'json', '--json-schema', json.dumps(schema),
            '--tools', '', '--strict-mcp-config', '--setting-sources', '',
            '--disable-slash-commands', '--no-session-persistence']
    try:
        with tempfile.TemporaryDirectory(prefix='e2e-judge-') as fresh:
            home, cwd = os.path.join(fresh, 'home'), os.path.join(fresh, 'cwd')
            os.makedirs(os.path.join(home, '.claude'))
            os.makedirs(cwd)
            # A private copy, never a link: a token refresh during the run
            # rewrites the copy, which is deleted with the temp dir, and never
            # writes through to the real file.
            with open(creds, 'rb') as src:
                secret = src.read()
            fd = os.open(os.path.join(home, '.claude', '.credentials.json'),
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as dst:
                dst.write(secret)
            env = {k: v for k, v in os.environ.items() if not k.startswith('CLAUDE')}
            env['HOME'] = home
            proc = subprocess.run(argv, input=prompt, capture_output=True, text=True,
                                  env=env, cwd=cwd, timeout=timeout)
        with open(raw, 'w', encoding='utf-8') as fh:
            fh.write(proc.stdout + proc.stderr)
    except (OSError, subprocess.SubprocessError) as exc:
        with open(raw, 'w', encoding='utf-8') as fh:
            fh.write(repr(exc))
        raise Inconclusive(f'{exc.__class__.__name__}; see {raw}') from exc
    try:
        verdict = json.loads(proc.stdout)['structured_output']
    except (ValueError, KeyError, TypeError) as exc:
        raise Inconclusive(f'no structured_output ({exc.__class__.__name__}); see {raw}') from exc
    if not isinstance(verdict, dict):
        raise Inconclusive(f'structured_output is not an object; see {raw}')
    return verdict
