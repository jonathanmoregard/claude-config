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

THE SERVING TREE IS A THIRD PATH, AND IT MUST NOT APPEAR AT ALL. Since
2026-10-27 the tree `~/.claude` is actually served from is a deploy-only clone
at `/var/lib/claude-runtime/checkout`, owned by the `claude-deploy` system user
and written only by `claude-runtime-deploy.service` (nixos-config,
`modules/nixos/claude-runtime-deploy.nix`). Jonathan's reason for the split:
"Repos/claude-config is the dev version, and .claude is the deployed artefact.
Real weird for production to depend on dev."

`protectedWorktrees` would be the wrong tool for that path and listing it would
be a category error with a real cost: the broker is the AGENT-FACING git tool,
so naming the serving tree in any of its roots tells it that tree is a place
agents operate — the precise coupling the split removed. The serving tree needs
no protection from the broker because no agent has write access to it at all;
that is enforced by file ownership, one layer down, and asserted behaviourally
in nixos-config's vm-base lane. So this guard also pins the ABSENCE.

WHY THE LIVE ENTRY STAYS, even though it stops matching anything. Membership is
an exact match on the resolved worktree, so once `~/.claude` holds no `.git` the
entry can never match and protects nothing — it is self-deactivating. Deleting
it is therefore cosmetic AFTER cutover and actively harmful BEFORE it, because
until the cutover runs `~/.claude` is still the live checkout and that entry is
what stops an agent committing in it. It costs nothing to keep (paths resolve
with `strict=False`), so it is kept and the cleanup belongs to the cutover
runbook, not to this change.

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
# The deploy-only clone the projection is served from. No agent can write
# it, so it must appear in none of the broker's path lists.
SERVING_TREE = "/var/lib/claude-runtime"


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


def _path_lists() -> dict[str, list[str]]:
    """Every list of paths the shipped policy carries, by key.

    Collected by walking the document rather than by naming the keys, so a new
    root added in a future edit is covered the day it appears instead of the
    day somebody remembers to extend this test.
    """
    found: dict[str, list[str]] = {}

    def walk(node: object, prefix: str = "") -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                here = f"{prefix}.{key}" if prefix else key
                if isinstance(value, list) and all(
                    isinstance(item, str) for item in value
                ):
                    if any(item.startswith("/") for item in value):
                        found[here] = value
                walk(value, here)

    walk(_config())
    return found


def test_serving_tree_is_in_no_path_list() -> None:
    offenders = {
        key: values
        for key, values in _path_lists().items()
        if any(value == SERVING_TREE or value.startswith(SERVING_TREE + "/") for value in values)
    }
    assert not offenders, (
        f"{SERVING_TREE} appears in {offenders}. That tree is served to every "
        "agent read-only and is writable only by the claude-deploy system "
        "user; naming it in a broker path list says agents operate there, "
        "which is the dev/production coupling the deploy split removed. "
        "Protection for it is file ownership, not policy."
    )
