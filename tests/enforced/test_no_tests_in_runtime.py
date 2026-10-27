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
`empirics-harness/run.sh` is a harness by content and `run.sh` by name, and
`mcps/github-mux/fake_child.py` was a stand-in child named like production
code, which review caught here and not this guard.

That limit is deliberate and the regex is NOT the place to fix it. The
invariant is that test code lives under `tests/`, and what holds it is moving
the files, not a classifier that grows a branch per surprise — each branch
added is one more thing to be wrong about, and the next surprise will not match
it either. The heuristic is paired with `ALLOWED`, which is empty: the same
bargain `runtime-layout.py` strikes by refusing an entry it does not recognise.
Adding an entry there is a decision someone has to write down and defend in
review, and that, not the regex, is what keeps this honest.

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


def is_test_path(path: Path) -> bool:
    """The whole decision, so the control below can exercise it directly."""
    if path.as_posix() in ALLOWED:
        return False
    return bool(RESERVED_DIRS.intersection(path.parts[:-1])
                or TEST_FILENAME.match(path.name))


def offenders() -> list[str]:
    return sorted(p.as_posix() for p in tracked_runtime_paths() if is_test_path(p))


def test_runtime_holds_no_test_code():
    # "Found nothing" and "scanned nothing" must not look alike. `git ls-files
    # runtime` answers empty for a tree that is untracked, for a REPO_ROOT that
    # resolved somewhere else, and for a clean one -- and only the last of
    # those is a pass. `lint-runtime-entrypoints.py` grew the same assertion
    # after it kept reporting clean against a tree it could no longer read.
    scanned = tracked_runtime_paths()
    assert len(scanned) > 100, (
        f"only {len(scanned)} tracked path(s) under runtime/ from "
        f"{REPO_ROOT} -- the scan did not reach the tree, so a clean result "
        "here would mean nothing"
    )
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
    for flagged in (
        "runtime/permission-ledger/test_hook.py",
        "runtime/skills/orchestrate/hooks/test-compact-reinject.sh",
        "runtime/skills/x/scripts/codex-advise-test.sh",
        "runtime/tools/ai-router/conftest.py",
        "runtime/mcps/x/server.test.ts",
        "runtime/skills/x/tests/anything.py",      # by directory, any name
        "runtime/tools/x/fixtures/sample.json",
    ):
        assert is_test_path(Path(flagged)), f"guard stopped flagging {flagged}"
    for kept in (
        "runtime/permission-ledger/hook.py",
        "runtime/permission-ledger/run-evaluate.sh",
        "runtime/scripts/agent-e2e/agent_e2e.py",
        "runtime/skills/advice-refine-test-loop/scripts/deterministic-gate.sh",
        # Live skills whose own names carry a reserved word: a directory
        # component has to BE one, not merely contain one.
        "runtime/skills/advice-refine-test-loop/SKILL.md",
        "runtime/skills/ab-testing-prompt-changes/ab_harness.py",
    ):
        assert not is_test_path(Path(kept)), f"guard now flags {kept}"
