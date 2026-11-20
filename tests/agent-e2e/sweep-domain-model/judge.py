"""Model-graded checks for sweep-domain-model: an Opus judge reads the sweep's
lens files against judge.md (the planted problems) and returns pass/evidence
per check. Prints "PASS ..." or "missing/bad: ..."; exits 2 when the judge
itself fails, so the harness reports INCONCLUSIVE instead of a verdict.

usage: judge.py SWEEP_DIR JUDGE_HOME CREDENTIALS_FILE
The judge runs in JUDGE_HOME, a fresh HOME holding only the credentials, with
no tools, no settings, no MCP and no skills: the config under test must not
shape its own grade.
"""
import glob
import json
import os
import subprocess
import sys

CHECKS = ['P1', 'P2', 'P3', 'P4', 'P5', 'G', 'X']
here = os.path.dirname(os.path.abspath(__file__))
sweep_dir, judge_home, creds = sys.argv[1:4]

files = sorted(f for f in glob.glob(os.path.join(sweep_dir, '*', '*.md')) if not f.endswith('inventory.md'))
if not files:
    print('missing/bad: no lens files')
    sys.exit(1)
output = '\n\n'.join(f'--- {os.path.basename(f)} ---\n' + open(f, encoding='utf-8').read() for f in files)
prompt = open(os.path.join(here, 'judge.md'), encoding='utf-8').read() + output

os.makedirs(os.path.join(judge_home, '.claude'), exist_ok=True)
link = os.path.join(judge_home, '.claude', '.credentials.json')
if not os.path.lexists(link):
    os.symlink(creds, link)

schema = {
    'type': 'object', 'additionalProperties': False, 'required': CHECKS,
    'properties': {c: {'type': 'object', 'additionalProperties': False, 'required': ['pass', 'evidence'],
                       'properties': {'pass': {'type': 'boolean'}, 'evidence': {'type': 'string'}}}
                   for c in CHECKS},
}
claude = os.environ.get('E2E_JUDGE_CLAUDE', 'claude')
argv = [claude, '-p', '--model', os.environ.get('E2E_JUDGE_MODEL', 'opus'), '--output-format', 'json',
        '--json-schema', json.dumps(schema), '--tools', '', '--strict-mcp-config', '--setting-sources', '',
        '--disable-slash-commands', '--no-session-persistence']
env = {k: v for k, v in os.environ.items() if not k.startswith('CLAUDE')}
env['HOME'] = judge_home
proc = subprocess.run(argv, input=prompt, capture_output=True, text=True, env=env, cwd=judge_home, timeout=600)
open(os.path.join(judge_home, 'judge.out'), 'w', encoding='utf-8').write(proc.stdout + proc.stderr)
try:
    result = json.loads(proc.stdout)
    verdicts = result.get('structured_output') or json.loads(result['result'])
    missing = [f"{c}:{verdicts[c]['evidence'][:160]}" for c in CHECKS if not verdicts[c]['pass']]
except (ValueError, KeyError, TypeError) as exc:
    print(f'judge failed ({exc.__class__.__name__}); see {judge_home}/judge.out')
    sys.exit(2)
print('PASS ' + ', '.join(CHECKS) + ' hold' if not missing else 'missing/bad: ' + ' | '.join(missing))
sys.exit(0 if not missing else 1)
