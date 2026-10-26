#!/usr/bin/env python3
"""Propose merging upstream changes into our forks: one proposal per fork.

For each entry in upstream-drift.json, fetch the fork branch and its upstream
branch into a private bare cache repo, list the upstream commits since their
merge-base that touch the entry's tracked paths, flag the ones touching
customized paths (merge-collision risk), and write <proposals-dir>/<name>.md
for /review-improvements to pick up.

Invariants:
  * Only the cache repo and the proposal file are written. Live checkouts
    (the fork's clone, ~/.claude/vendor/*) are never read or touched.
  * A proposal's identity is the newest upstream commit on a tracked path
    (`upstream_sha`). Any proposal under the proposals dir, archived ones
    included and at any status, that records that commit for the entry is
    left untouched. A newer relevant commit rewrites <name>.md as pending.
  * No upstream commit on a tracked path since the merge-base: nothing is
    written and nothing is removed.
  * An entry that cannot be checked (fetch failure, unrelated histories) is
    reported on stderr and makes the exit status nonzero; the other entries
    still run.

The frontmatter is read as flat top-level `key: value` lines, the same way
the proposal counter does; this script only ever needs `name` and
`upstream_sha` back out of it.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime
import json
import pathlib
import re
import subprocess
import sys
import tempfile

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "upstream-drift.json"
DEFAULT_CACHE_DIR = pathlib.Path("~/.cache/upstream-drift").expanduser()
DEFAULT_PROPOSALS_DIR = pathlib.Path("~/.local/state/claude-proposals/drift").expanduser()

FORK_REF = "refs/drift/fork"
UPSTREAM_REF = "refs/drift/upstream"


@dataclasses.dataclass(frozen=True)
class Commit:
    sha: str
    date: str
    subject: str
    paths: list[str]
    collisions: list[str]  # the customized paths this commit touches


def git(cache: pathlib.Path, *args: str) -> str:
    """Run git on the bare cache repo. --git-dir, not -C: with
    safe.bareRepository=explicit git refuses to discover a bare repo."""
    return subprocess.run(["git", f"--git-dir={cache}", *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def fetch(cache: pathlib.Path, url: str, branch: str, ref: str) -> str:
    """Fetch URL's BRANCH into REF of the cache repo; return its commit."""
    git(cache, "fetch", "--quiet", "--no-tags", url, f"+refs/heads/{branch}:{ref}")
    return git(cache, "rev-parse", ref)


def under(path: str, prefixes: list[str]) -> list[str]:
    """The prefixes (directories or files) that PATH lies under."""
    return [p for p in prefixes if path == p or path.startswith(p.rstrip("/") + "/")]


def relevant_commits(cache: pathlib.Path, base: str, head: str,
                     tracked: list[str], customized: list[str]) -> list[Commit]:
    """Non-merge commits in base..head touching TRACKED, newest first."""
    log = git(cache, "log", "--no-merges", "--format=%x00%H%x1f%cs%x1f%s",
              "--name-only", f"{base}..{head}", "--", *tracked)
    commits = []
    for chunk in filter(None, log.split("\0")):
        header, *files = chunk.strip("\n").split("\n")
        sha, date, subject = header.split("\x1f", 2)
        paths = [f for f in files if f]
        hits = sorted({p for f in paths for p in under(f, customized)})
        commits.append(Commit(sha, date, subject, paths, hits))
    return commits


def frontmatter(path: pathlib.Path) -> dict[str, str]:
    """Top-level scalar keys of a proposal's frontmatter ({} when it has none)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    m = re.match(r"\A---\n(.*?)^---$", text, re.DOTALL | re.MULTILINE)
    if not m:
        return {}
    pairs = re.findall(r"^([A-Za-z_][\w-]*):[ \t]*(.*?)[ \t]*$", m.group(1), re.MULTILINE)
    return {k: v.strip("'\"") for k, v in pairs}


def already_proposed(proposals_dir: pathlib.Path, name: str, upstream_sha: str) -> pathlib.Path | None:
    """A proposal (live or archived, any status) recording UPSTREAM_SHA for NAME."""
    for path in sorted(proposals_dir.rglob("*.md")):
        fm = frontmatter(path)
        if fm.get("name") == name and fm.get("upstream_sha") == upstream_sha:
            return path
    return None


def render(entry: dict, commits: list[Commit], base: str, fork_sha: str,
           upstream_head: str, today: datetime.date) -> str:
    name, upstream, fork = entry["name"], entry["upstream_url"], entry["fork_repo"]
    ub, fb = entry["upstream_branch"], entry["fork_branch"]
    collisions = [c for c in commits if c.collisions]

    lines = [
        "---",
        "status: pending",
        "category: drift",
        f"date: {today.isoformat()}",
        "source: upstream-drift",
        f"name: {name}",
        f"upstream_sha: {commits[0].sha}",
        f"fork_sha: {fork_sha}",
        "---",
        f"# Upstream drift: {name}",
        "",
        (f"Upstream {upstream} `{ub}` (checked at `{upstream_head[:12]}`) has "
         f"**{len(commits)}** commit(s) touching tracked paths since its merge-base "
         f"`{base[:12]}` with the fork {fork} `{fb}` (at `{fork_sha[:12]}`). "
         f"**{len(collisions)}** of them touch customized paths (merge-collision risk)."),
        "",
        "## Commits",
        "",
    ]
    for i, c in enumerate(commits, 1):
        lines.append(f"{i}. `{c.sha[:12]}` {c.date} {c.subject}")
        for p in c.paths:
            mark = " (customized)" if under(p, entry["customized_paths"]) else ""
            lines.append(f"   - `{p}`{mark}")
    lines += ["", "## Collisions", ""]
    if collisions:
        for i, c in enumerate(collisions, 1):
            touched = ", ".join(f"`{p}`" for p in c.collisions)
            lines.append(f"{i}. `{c.sha[:12]}` {c.subject}: {touched}")
    else:
        lines.append("None: no upstream commit touches a customized path.")
    customized = ", ".join(f"`{p}`" for p in entry["customized_paths"])
    pin = entry.get("pin")
    lines += [
        "",
        "## How to act",
        "",
        (f"1. In a fresh worktree of the fork ({fork}), branch off `{fb}` and run "
         f"`git fetch {upstream} {ub} && git merge FETCH_HEAD`."),
        (f"2. Resolve conflicts preserving our customizations in {customized}; "
         "take upstream's changes everywhere else. Work through the collisions above "
         "first: upstream changed what we customized, so fold their improvement into "
         "our version rather than picking one side."),
        "3. Push the branch and open a PR on the fork; the merge click is Jonathan's.",
        (f"4. Once merged, scripts/update-submodules.sh opens the ~/.claude PR that "
         f"moves the `{pin}` pin (weekly), or bump it by hand in a ~/.claude worktree."
         if pin else "4. Once merged, move whatever pins the fork to the new commit."),
        ("5. Mark this proposal `status: implemented` with a `resolution:` naming the "
         "fork PR. The next run proposes again only when upstream moves on a tracked path."),
        "",
    ]
    return "\n".join(lines)


def write_atomically(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".", suffix=".tmp",
                                     delete=False, encoding="utf-8") as tmp:
        tmp.write(text)
    pathlib.Path(tmp.name).replace(path)


def check(entry: dict, cache_dir: pathlib.Path, proposals_dir: pathlib.Path,
          today: datetime.date) -> str:
    """Check one entry; write or leave its proposal. Returns a one-line summary."""
    name = entry["name"]
    cache = cache_dir / f"{name}.git"
    if not cache.is_dir():
        cache.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "--quiet", "--bare", str(cache)], check=True,
                       capture_output=True, text=True)
    fork_sha = fetch(cache, entry["fork_repo"], entry["fork_branch"], FORK_REF)
    upstream_head = fetch(cache, entry["upstream_url"], entry["upstream_branch"], UPSTREAM_REF)
    base = git(cache, "merge-base", fork_sha, upstream_head)
    commits = relevant_commits(cache, base, upstream_head,
                               entry["tracked_paths"], entry["customized_paths"])
    if not commits:
        return f"{name}: no upstream commits on tracked paths since {base[:12]}"
    existing = already_proposed(proposals_dir, name, commits[0].sha)
    if existing:
        return f"{name}: unchanged, {existing} records upstream {commits[0].sha[:12]}"
    path = proposals_dir / f"{name}.md"
    write_atomically(path, render(entry, commits, base, fork_sha, upstream_head, today))
    collisions = sum(1 for c in commits if c.collisions)
    return f"{name}: wrote {path} ({len(commits)} commits, {collisions} colliding)"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", type=pathlib.Path, default=DEFAULT_CONFIG)
    ap.add_argument("--cache-dir", type=pathlib.Path, default=DEFAULT_CACHE_DIR)
    ap.add_argument("--proposals-dir", type=pathlib.Path, default=DEFAULT_PROPOSALS_DIR)
    args = ap.parse_args(argv)

    entries = json.loads(args.config.read_text(encoding="utf-8"))["entries"]
    today = datetime.datetime.now().astimezone().date()
    rc = 0
    for entry in entries:
        try:
            print(check(entry, args.cache_dir, args.proposals_dir, today))
        except subprocess.CalledProcessError as e:
            rc = 1
            detail = (e.stderr or "").strip().splitlines()[-1:] or [str(e)]
            print(f"{entry['name']}: check failed: {detail[0]}", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
