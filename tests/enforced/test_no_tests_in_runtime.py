#!/usr/bin/env python3
"""No test code inside `runtime/`, because `runtime/` is what becomes live.

WHAT IT GUARDS. `scripts/runtime-layout.py` keeps the top-level `tests/` out of
the projection, but it decides per top-level entry and says nothing about what
sits further down. A `tests/` directory nested inside a projected component is
carried into `~/.claude` along with it, which is how 50 files of test code —
including a test script sitting in a skill's `hooks/` directory — ended up at
live paths while the partition looked complete.

Read-only mounts mean none of it can be edited in place, so the write side of
the property holds either way. What this guard is for is the reach side: test
code at a live path is test code a hook, a skill or an MCP server can name.

HOW IT DECIDES, and the limit of that. The check is a filename and directory
heuristic, so it recognises the shapes this repository actually uses rather
than proving a file is a test. A name nobody anticipated slips past: an
`empirics-harness/run.sh` is a harness by content and `run.sh` by name. The
heuristic is therefore paired with `ALLOWED`, which is empty — the same bargain
`runtime-layout.py` strikes by refusing an entry it does not recognise. Adding
an entry there is a decision someone has to write down and defend in review,
and that, not the regex, is what keeps this honest.

A directory component has to BE one of the reserved names, not merely contain
one: `advice-refine-test-loop` and `ab-testing-prompt-changes` are live skills
whose own names carry the word.

`tests/enforced/` is the directory the required `checks` job runs whole.

Run:  nix shell nixpkgs#python3Packages.pytest -c pytest tests/enforced -q
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

RESERVED_DIRS = {"tests", "test", "fixtures"}

TEST_FILENAME = re.compile(
    r"""^test[_-].+\.(py|sh)$      # test_hook.py, test-compact-reinject.sh
      | ^.+[_-]test\.(py|sh)$      # codex-advise-test.sh, foo_test.py
      | ^conftest\.py$
      | ^.+\.test\.[^.]+$          # foo.test.ts
    """,
    re.VERBOSE,
)

# Deliberate exceptions. Each needs its reason written here, and the reason has
# to be that the file is NOT test code despite its shape — never that moving it
# is inconvenient.
ALLOWED: set[str] = set()


def tracked_runtime_paths() -> list[Path]:
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "runtime"],
        capture_output=True, text=True, check=True,
    )
    return [Path(line) for line in result.stdout.splitlines() if line]


def offenders() -> list[str]:
    found = []
    for path in tracked_runtime_paths():
        rel = path.as_posix()
        if rel in ALLOWED:
            continue
        in_reserved_dir = RESERVED_DIRS.intersection(path.parts[:-1])
        if in_reserved_dir or TEST_FILENAME.match(path.name):
            found.append(rel)
    return sorted(found)


def test_runtime_holds_no_test_code():
    found = offenders()
    assert not found, (
        f"{len(found)} test file(s) tracked under runtime/, which the projection "
        "carries into ~/.claude:\n  " + "\n  ".join(found)
    )


def test_the_heuristic_still_recognises_the_shapes_it_is_for():
    """A guard that cannot fire is not a guard.

    Without this, deleting a branch of the regex would leave the suite green
    for the wrong reason, and the only symptom would be test code drifting
    back into the live tree unnoticed.
    """
    for name in ("test_hook.py", "test-compact-reinject.sh",
                 "codex-advise-test.sh", "conftest.py", "server.test.ts"):
        assert TEST_FILENAME.match(name), f"heuristic stopped recognising {name}"
    for name in ("hook.py", "run-evaluate.sh", "agent_e2e.py",
                 "deterministic-gate.sh", "SKILL.md"):
        assert not TEST_FILENAME.match(name), f"heuristic now flags {name}"
    assert RESERVED_DIRS.intersection(Path("runtime/x/tests/y.py").parts[:-1])
    assert not RESERVED_DIRS.intersection(
        Path("runtime/skills/advice-refine-test-loop/SKILL.md").parts[:-1]
    )
