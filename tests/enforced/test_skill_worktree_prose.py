#!/usr/bin/env python3
"""Agent-facing prose must not tell an agent to branch off shared local main.

The worktree policy is that a new branch is based on `origin/main` (or
`origin/master`) after a fetch, so no automation depends on or advances the
shared local branch. Scripts are checked by `tests/test_worktree_policy.sh`;
this file checks the *instructions*, which is a separate failure: a skill that
says `git worktree add -b feat/<slug> main` produces the wrong branch every
time, and no script test can see it.

Ported from nixos-config's `tests/base.nix`, which held this assertion for the
four nixos-* skills while home-manager owned them. Those skills are real files
in `runtime/skills/` now, so the check has to live here or it is lost -- and
applying it to every skill rather than a named four is strictly better than
what it replaces.

Run:  nix shell nixpkgs#python3Packages.pytest -c pytest tests/enforced -q
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# `-b <branch> main` with `main`/`master` as a bare word: the start point is
# the shared local branch. `origin/main` does not match, because the word is
# preceded by `/` rather than whitespace -- which is the whole distinction
# being enforced.
STALE_BASE = re.compile(r"-b\s+\S+\s+(main|master)\b")


def agent_prose() -> list[Path]:
    """Every instruction file an agent loads out of the projection."""
    paths = sorted((REPO_ROOT / "runtime" / "skills").glob("*/SKILL.md"))
    for extra in ["CLAUDE.md"]:
        candidate = REPO_ROOT / "runtime" / extra
        if candidate.is_file():
            paths.append(candidate)
    return paths


def test_there_is_prose_to_check():
    """Guard the guard: a glob that matches nothing passes every assertion."""
    paths = agent_prose()
    assert len(paths) > 10, [str(p.relative_to(REPO_ROOT)) for p in paths]


@pytest.mark.parametrize("relative", [
    str(p.relative_to(REPO_ROOT)) for p in agent_prose()
])
def test_no_skill_bases_a_worktree_on_shared_local_main(relative):
    path = REPO_ROOT / relative
    # Symlinks into vendor/ are somebody else's prose; an uninitialised
    # submodule makes them dangling, which is not this suite's business.
    if path.is_symlink() and not path.exists():
        pytest.skip(f"{relative} is a dangling symlink (submodule not initialised)")
    offenders = [
        f"{n}: {line.strip()}"
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if STALE_BASE.search(line)
    ]
    assert not offenders, (
        f"{relative} tells an agent to base a new branch on the shared local "
        "branch instead of origin/<default> after a fetch:\n  "
        + "\n  ".join(offenders)
    )
