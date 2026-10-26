#!/usr/bin/env python3
"""Rewrite github-mux's `defaultBranches` from GitHub.

The mux refuses a pull request whose base is not its repository's own default
branch, and it will not guess: a repository the map does not name is refused.
This keeps the map honest. Run it when that refusal names a repository you
expect to work, or after a repository renames its default branch, then commit
the result.

    python3 mcps/github-mux/refresh-default-branches.py

Owners come from the config itself -- `defaultOwners` plus every key in
`ownerChildren` -- so adding an installation does not mean editing this script.
Needs `gh` authenticated for each of those owners.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

CONFIG = Path(__file__).resolve().parent / "config.json"


def owner_repos(owner: str) -> dict[str, str]:
    out = subprocess.run(
        [
            "gh",
            "repo",
            "list",
            owner,
            "--limit",
            "1000",
            "--json",
            "nameWithOwner,defaultBranchRef",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    found: dict[str, str] = {}
    for repo in json.loads(out):
        ref = repo.get("defaultBranchRef") or {}
        branch = ref.get("name")
        # An empty repository has no default branch ref yet; it also cannot
        # receive a pull request, so leaving it out costs nothing.
        if branch:
            found[repo["nameWithOwner"].lower()] = branch
    return found


def main() -> int:
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    owners = [*config["defaultOwners"], *config["ownerChildren"]]
    branches: dict[str, str] = {}
    for owner in owners:
        branches.update(owner_repos(owner))
    if not branches:
        print("refresh-default-branches: gh returned no repositories", file=sys.stderr)
        return 1
    config["defaultBranches"] = dict(sorted(branches.items()))
    CONFIG.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    print(f"refresh-default-branches: {len(branches)} repositories from {owners}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
