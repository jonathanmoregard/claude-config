"""Model-graded checks for sweep-domain-model: an Opus judge reads the sweep's
lens files and final message against judge.md (the planted problems) and
returns pass/evidence per check. Prints "PASS ..." or "missing/bad: ...";
exits 2 when the judge itself fails, so the harness reports INCONCLUSIVE
instead of a verdict.

usage: judge.py SWEEP_DIR [FINAL_TXT]
The judge's HOME and working directory are fresh, its raw output goes to a
fresh directory (named in an INCONCLUSIVE result), and it has no tools, settings, MCP or
skills, so the config under test cannot shape its grade. Its credentials are
the real (passwd) home's file; the harness mounts that file writable into
the sandbox for token refresh, so an agent could spoil it, but that only
makes the judge fail to authenticate (INCONCLUSIVE), never pass.
"""
import glob
import json
import os
import pwd
import subprocess
import sys
import tempfile

CHECKS = ['P1', 'P2', 'P3', 'P4', 'P5', 'G', 'X', 'V']
here = os.path.dirname(os.path.abspath(__file__))
sweep_dir = sys.argv[1]
final_path = sys.argv[2] if len(sys.argv) > 2 else ''
creds = os.path.join(pwd.getpwuid(os.getuid()).pw_dir, '.claude', '.credentials.json')
out_dir = tempfile.mkdtemp(prefix='sweep-judge-out-')


def inconclusive(why):
    print(f'judge failed ({why}); see {out_dir}/judge.out')
    sys.exit(2)


def read(path):
    return open(path, encoding='utf-8', errors='replace').read()


files = sorted(f for f in glob.glob(os.path.join(sweep_dir, '*', '*.md')) if not f.endswith('inventory.md'))
if not files:
    print('missing/bad: no lens files')
    sys.exit(1)
prompt = (read(os.path.join(here, 'judge.md'))
          + '\n\n'.join(f'--- {os.path.basename(f)} ---\n' + read(f) for f in files)
          + "\n\n=== THE SWEEP AGENT'S FINAL MESSAGE ===\n"
          + (read(final_path) if final_path and os.path.exists(final_path) else ''))

schema = {
    'type': 'object', 'additionalProperties': False, 'required': CHECKS,
    'properties': {c: {'type': 'object', 'additionalProperties': False, 'required': ['pass', 'evidence'],
                       'properties': {'pass': {'type': 'boolean'}, 'evidence': {'type': 'string'}}}
                   for c in CHECKS},
}
argv = [os.environ.get('E2E_JUDGE_CLAUDE', 'claude'), '-p', '--model', os.environ.get('E2E_JUDGE_MODEL', 'opus'),
        '--output-format', 'json', '--json-schema', json.dumps(schema), '--tools', '', '--strict-mcp-config',
        '--setting-sources', '', '--disable-slash-commands', '--no-session-persistence']
try:
    with tempfile.TemporaryDirectory(prefix='sweep-judge-') as fresh:
        home, cwd = os.path.join(fresh, 'home'), os.path.join(fresh, 'cwd')
        os.makedirs(os.path.join(home, '.claude'))
        os.makedirs(cwd)
        os.symlink(creds, os.path.join(home, '.claude', '.credentials.json'))
        env = {k: v for k, v in os.environ.items() if not k.startswith('CLAUDE')}
        env['HOME'] = home
        proc = subprocess.run(argv, input=prompt, capture_output=True, text=True, env=env, cwd=cwd, timeout=600)
    open(os.path.join(out_dir, 'judge.out'), 'w', encoding='utf-8').write(proc.stdout + proc.stderr)
except (OSError, subprocess.SubprocessError) as exc:
    inconclusive(exc.__class__.__name__)
try:
    verdicts = json.loads(proc.stdout)['structured_output']
    if not all(type(verdicts[c]['pass']) is bool and isinstance(verdicts[c]['evidence'], str) for c in CHECKS):
        inconclusive('malformed verdict')
except (ValueError, KeyError, TypeError) as exc:
    inconclusive(exc.__class__.__name__)
missing = [f"{c}:{verdicts[c]['evidence'][:160]}" for c in CHECKS if not verdicts[c]['pass']]
print('PASS ' + ', '.join(CHECKS) + ' hold' if not missing else 'missing/bad: ' + ' | '.join(missing))
sys.exit(0 if not missing else 1)
