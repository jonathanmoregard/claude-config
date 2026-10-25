#!/usr/bin/env python3
"""SessionStart hook: put this repo's git worktrees in the session's scope.

Feature work happens in worktrees (`~/worktrees/<repo>-<slug>/`, or wherever
`git worktree add` put them). A session started in the main checkout can't
touch them without a permission prompt per file, which is friction on exactly
the flow that's supposed to be frictionless.

So: enumerate the worktrees of the repo this session was started in that still
hold work (dirty, or commits on no remote's default branch), and write them into `<project>/.claude/settings.local.json` under
`permissions.additionalDirectories`. That file is gitignored globally
(`~/.config/git/ignore`), so nothing lands in the repo.

The list we manage is remembered under `_managedWorktreeDirs`, so a worktree
that gets removed is dropped again on the next session instead of accreting
forever. Directories added by hand are never touched.

Always exits 0 — a broken hook must not be able to stop a session starting.
"""

import concurrent.futures
import json
import os
import pathlib
import subprocess
import sys

MANAGED_KEY = "_managedWorktreeDirs"


def project_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())


def worktrees_of(repo: pathlib.Path) -> list[str]:
    """Absolute paths of every worktree of `repo`, excluding `repo` itself."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []  # not a git repo — nothing to do
    here = repo.resolve()
    found = []
    for line in out.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        path = pathlib.Path(line[len("worktree "):].strip())
        if not path.is_dir():
            continue  # pruned/stale entry
        resolved = path.resolve()
        if resolved == here:
            continue  # the checkout we're already in
        found.append(str(resolved))
    bases = default_refs(repo)
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        live = list(pool.map(lambda p: has_work(p, bases), found))
    return sorted({p for p, keep in zip(found, live) if keep})


def git(path: str, *args: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", path, *args], capture_output=True,
                             text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def default_refs(repo: pathlib.Path) -> list[str]:
    """`<remote>/<default>` for every remote that has one. A repo renamed with
    rewritten history keeps its old remote, whose merged work is still merged."""
    refs = []
    for remote in (git(str(repo), "remote") or "").split():
        ref = git(str(repo), "symbolic-ref", "-q", "--short", f"refs/remotes/{remote}/HEAD")
        if ref and ref.strip():
            refs.append(ref.strip())
            continue
        for name in (f"{remote}/main", f"{remote}/master"):
            if git(str(repo), "rev-parse", "-q", "--verify", name) is not None:
                refs.append(name)
                break
    return refs


def has_work(path: str, bases: list[str]) -> bool:
    """Keep a worktree in scope while it holds work: uncommitted changes, or
    commits on no remote's default branch. A clean worktree whose branch is
    merged only costs environment-block tokens in every session. Unknown ->
    keep, so a git hiccup never hides live work."""
    status = git(path, "status", "--porcelain")
    if status is None or status.strip():
        return True
    if not bases:
        return True
    ahead = git(path, "rev-list", "--count", "HEAD", "--not", *bases)
    return ahead is None or ahead.strip() != "0"


def main() -> int:
    try:
        sys.stdin.read()  # drain the hook payload; we don't need it
    except OSError:
        pass

    repo = project_dir()
    wanted = worktrees_of(repo)

    settings_path = repo / ".claude" / "settings.local.json"
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.is_file() else {}
        if not isinstance(data, dict):
            return 0  # someone put something odd there — leave it alone
    except (OSError, ValueError):
        return 0

    perms = data.get("permissions")
    if not isinstance(perms, dict):
        perms = {}
    existing = [d for d in perms.get("additionalDirectories") or [] if isinstance(d, str)]
    previously_managed = [d for d in data.get(MANAGED_KEY) or [] if isinstance(d, str)]

    # Hand-added entries survive; ours are recomputed from the live worktree list.
    manual = [d for d in existing if d not in previously_managed]
    merged = manual + [d for d in wanted if d not in manual]

    if merged == existing and previously_managed == wanted:
        return 0  # nothing changed — stay silent

    perms["additionalDirectories"] = merged
    data["permissions"] = perms
    if wanted:
        data[MANAGED_KEY] = wanted
    else:
        data.pop(MANAGED_KEY, None)

    try:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = settings_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, settings_path)
    except OSError:
        return 0

    added = [d for d in wanted if d not in previously_managed]
    dropped = [d for d in previously_managed if d not in wanted]
    parts = []
    if added:
        parts.append(f"added {len(added)}: " + ", ".join(pathlib.Path(d).name for d in added))
    if dropped:
        parts.append(f"dropped {len(dropped)}: " + ", ".join(pathlib.Path(d).name for d in dropped))
    if parts:
        print(f"[worktrees] in scope for this session — {' · '.join(parts)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
