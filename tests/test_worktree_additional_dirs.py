#!/usr/bin/env python3
"""worktree-additional-dirs.py scopes only worktrees that still hold work.

Every listed directory lands in each session's environment block, so ~70
merged, clean worktrees cost tokens on every start. Checked against a sandbox
repo with an origin:

  * a dirty worktree is kept;
  * a worktree with commits not on origin's default branch is kept;
  * a clean worktree whose branch is merged is dropped;
  * merged means on any remote's default branch, so a renamed repo's old
    remote (`legacy`, unrelated history) still counts;
  * hand-added additionalDirectories survive.

Run: python3 tests/test_worktree_additional_dirs.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# `runtime/` once the layout migration has landed, the repository root before
# it. Only the projected subset moves; tests/ stays at the root.
RUNTIME = Path(__file__).resolve().parents[1]
if (RUNTIME / "runtime").is_dir():
    RUNTIME = RUNTIME / "runtime"

HOOK = RUNTIME / "worktree-additional-dirs.py"
FAILURES: list[str] = []
ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK ' if cond else 'FAIL'}] {label}" + (f" :: {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(label)


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, env=ENV, check=True, capture_output=True)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        origin = root / "origin.git"
        git(root, "init", "-q", "--bare", "-b", "main", str(origin))
        repo = root / "repo"
        git(root, "clone", "-q", str(origin), str(repo))
        git(repo, "commit", "-q", "--allow-empty", "-m", "init")
        git(repo, "push", "-q", "origin", "HEAD:main")
        git(repo, "remote", "set-head", "origin", "main")

        merged, dirty, ahead = (root / n for n in ("wt-merged", "wt-dirty", "wt-ahead"))
        for wt, br in ((merged, "merged"), (dirty, "dirty"), (ahead, "ahead")):
            git(repo, "worktree", "add", "-q", "-b", br, str(wt), "origin/main")
        (dirty / "wip.txt").write_text("x")
        git(ahead, "commit", "-q", "--allow-empty", "-m", "unpushed")

        # A renamed repo with rewritten history: the old remote stays as
        # `legacy`, sharing no commits with origin. Work merged there is merged.
        legacy = root / "legacy.git"
        git(root, "init", "-q", "--bare", "-b", "master", str(legacy))
        old = root / "old"
        git(root, "clone", "-q", str(legacy), str(old))
        git(old, "commit", "-q", "--allow-empty", "-m", "old history")
        git(old, "push", "-q", "origin", "HEAD:master")
        git(repo, "remote", "add", "legacy", str(legacy))
        git(repo, "fetch", "-q", "legacy")
        legacy_merged, legacy_ahead = root / "wt-legacy-merged", root / "wt-legacy-ahead"
        for wt, br in ((legacy_merged, "legacy-merged"), (legacy_ahead, "legacy-ahead")):
            git(repo, "worktree", "add", "-q", "-b", br, str(wt), "legacy/master")
        git(legacy_ahead, "commit", "-q", "--allow-empty", "-m", "unmerged on legacy")
        # A remote HEAD left dangling (default branch renamed, then pruned)
        # must neither poison the other refs nor hide legacy/master.
        git(repo, "symbolic-ref", "refs/remotes/legacy/HEAD", "refs/remotes/legacy/gone")

        manual = str(root / "hand-added")
        (repo / ".claude").mkdir()
        settings = repo / ".claude" / "settings.local.json"
        settings.write_text(json.dumps({"permissions": {"additionalDirectories": [manual]}}))

        subprocess.run([sys.executable, str(HOOK)], input="{}", text=True, capture_output=True,
                       env={**ENV, "CLAUDE_PROJECT_DIR": str(repo)}, timeout=60)
        dirs = json.loads(settings.read_text())["permissions"]["additionalDirectories"]
        check("dirty worktree kept", str(dirty) in dirs, str(dirs))
        check("worktree with unpushed commits kept", str(ahead) in dirs, str(dirs))
        check("clean merged worktree dropped", str(merged) not in dirs, str(dirs))
        check("hand-added entry survives", manual in dirs, str(dirs))
        check("worktree merged on another remote's default branch dropped",
              str(legacy_merged) not in dirs, str(dirs))
        check("worktree ahead of every remote's default branch kept",
              str(legacy_ahead) in dirs, str(dirs))

    print("all passed" if not FAILURES else f"{len(FAILURES)} failed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
