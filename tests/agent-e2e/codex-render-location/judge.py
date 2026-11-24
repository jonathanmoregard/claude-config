"""Judge for codex-render-location: which repo and directory does the answer
tell the user to edit? The judge returns enum fields through --json-schema
(see ../_lib/structured_judge.py); this script compares fields, never text.

usage: judge.py RUN
RUN/final.txt is read only as a regular file reached without a symlink
(../_lib/run_files.py): check.sh runs on the host and the run dir is the
agent's to write.
Prints "PASS ..." / "FAIL ..." and exits 0 / 1; exits 2 when the judge fails.
"""
import os
from pathlib import Path
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '_lib'))
try:
    from run_files import Refused, read_run
    from structured_judge import Inconclusive, judge
except Exception as exc:  # a grader that cannot load is no verdict, never a FAIL
    print(f'INCONCLUSIVE: grader cannot load: {exc.__class__.__name__}: {exc}')
    sys.exit(2)

LIMIT = 256 * 1024  # bytes; an answer is far smaller

REPOS = ['claude-config', 'live-claude-dir', 'ai-client-config', 'other', 'none']
DIRS = ['codex', 'runtime', 'other', 'none']

INSTRUCTIONS = """You grade one answer. The user asked an assistant: "I want Codex to get my
Claude hooks mapped a bit differently — a Codex-only rendering change, nothing
about Claude itself. Which git repository and which directory inside it do I
edit?"

Report what the ANSWER tells the user to edit — the place it sends them to make
the change. A repository or directory the answer mentions only to rule it out,
as background, or as where output ends up does not count.

Fields:
- repo: the git repository the answer sends the user to edit.
  "claude-config" (the config git repo itself, e.g. ~/Repos/claude-config or a
  ~/worktrees/claude-* worktree of it),
  "live-claude-dir" (~/.claude or any path under it: the live read-only
  projection, not the repo, even if the answer calls it the Claude config),
  "ai-client-config" (a repo named ai-client-config), "other" (any other repo),
  "none" (the answer names no repository to edit, or is ambiguous between several).
- directory: the top-level directory inside that repository the answer sends
  the user to edit.
  "codex" (the repo's top-level codex/ directory, or a path under it such as
  codex/scripts/),
  "runtime" (runtime/ or anything under it, including runtime/codex/),
  "other" (any other directory), "none" (no directory, or ambiguous).
- quote: the sentence of the answer that names the edit target, copied verbatim.

=== THE ANSWER ===
"""

SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['repo', 'directory', 'quote'],
    'properties': {
        'repo': {'type': 'string', 'enum': REPOS},
        'directory': {'type': 'string', 'enum': DIRS},
        'quote': {'type': 'string'},
    },
}


def grade(answer: str) -> dict:
    """The judge's schema-checked verdict on one answer; raises Inconclusive."""
    v = judge(INSTRUCTIONS + answer, SCHEMA)
    if v.get('repo') not in REPOS or v.get('directory') not in DIRS or not isinstance(v.get('quote'), str):
        raise Inconclusive(f'malformed verdict {v!r}')
    return v


def passes(v: dict) -> bool:
    return v['repo'] == 'claude-config' and v['directory'] == 'codex'


def main() -> int:
    run = Path(sys.argv[1])
    try:
        raw = read_run(run, 'final.txt', LIMIT)
    except Refused as exc:
        print(f'FAIL: refused run content: {exc}')
        return 1
    if not raw:
        print('INCONCLUSIVE: no final message')
        return 2
    try:
        v = grade(raw.decode('utf-8', 'replace'))
    except Inconclusive as exc:
        print(f'INCONCLUSIVE: judge failed: {exc}')
        return 2
    got = f"repo={v['repo']} directory={v['directory']} quote={v['quote'][:200]!r}"
    if passes(v):
        print(f'PASS: {got}')
        return 0
    print(f'FAIL: {got}')
    return 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:  # a grader crash is no verdict: never let it read as FAIL
        print(f'INCONCLUSIVE: grader error: {exc.__class__.__name__}: {exc}')
        sys.exit(2)
