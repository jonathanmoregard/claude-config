#!/usr/bin/env python3
"""git-broker must protect the config checkout wherever that checkout lives.

WHAT IT GUARDS. `mcps/git-broker/config.json` lists `protectedWorktrees`, and
the broker refuses git mutations whose worktree is in that list
(`server.py`, `Git mutation denied in protected primary worktree`). The list
names absolute paths, and membership is an exact match on the resolved
worktree — not a prefix test — so a path that stops being a checkout stops
protecting anything, silently.

That is exactly what the runtime-projection cutover does to
`/home/jonathan/.claude`: afterwards it is a tree of symlinks and read-only
mounts with no `.git`, while the real checkout — writable, committable,
pushable, and the thing the projection is built from — sits at
`/home/jonathan/Repos/claude-config`. An entry naming only the live path would
leave the protection pointing at a non-repository and the repository itself
protected by nothing.

So both paths are required, and this is not transitional duplication:

  * before cutover, the live path IS the checkout and must stay protected;
  * after it, the backing path is, and an agent that writes there reaches the
    live tree on the next projection — an untracked file under
    `runtime/` is projected and `test_no_tests_in_runtime.py` never sees it,
    because that guard reads `git ls-files`.

Keeping the live entry after cutover costs nothing: paths are resolved with
`strict=False` (`server.py`), so naming a path that does not exist, or no
longer holds a repository, is not an error.

WHY HERE. Every test under `tests/mcps/git-broker/` builds its own policy
dict, which is right for behaviour but means none of them reads the config
this repository actually ships. `tests/enforced` is the suite CI runs, and
this is a property of the shipped file, not of the server's logic.

WHAT IT DOES NOT CHECK. That the broker enforces the list — `tests/mcps/
git-broker/test_git_broker.py` covers that against a synthesised policy. This
guard is about the contents of one tracked file.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

LIVE_CONFIG_DIR = "/home/jonathan/.claude"
BACKING_CHECKOUT = "/home/jonathan/Repos/claude-config"


def _config() -> dict:
    for candidate in (
        REPO_ROOT / "runtime" / "mcps" / "git-broker" / "config.json",
        REPO_ROOT / "mcps" / "git-broker" / "config.json",
    ):
        if candidate.exists():
            return json.loads(candidate.read_text(encoding="utf-8"))
    raise AssertionError("git-broker config.json not found in either layout")


def _protected() -> list[str]:
    # The key sits under a named section (`gitBroker` today). Found by search
    # rather than by a hardcoded section name, so renaming the wrapper does not
    # turn this guard into a silent pass.
    config = _config()
    if "protectedWorktrees" in config:
        return config["protectedWorktrees"]
    for section in config.values():
        if isinstance(section, dict) and "protectedWorktrees" in section:
            return section["protectedWorktrees"]
    raise AssertionError(f"no protectedWorktrees anywhere in {sorted(config)}")


def test_live_config_dir_is_protected() -> None:
    assert LIVE_CONFIG_DIR in _protected()


def test_backing_checkout_is_protected() -> None:
    protected = _protected()
    assert BACKING_CHECKOUT in protected, (
        f"{BACKING_CHECKOUT} is missing from protectedWorktrees {protected}. "
        "After the runtime-projection cutover that path is the writable config "
        "checkout, and an agent writing there reaches the live tree through the "
        "projection."
    )


def test_protected_paths_are_absolute() -> None:
    for path in _protected():
        assert Path(path).is_absolute(), path
