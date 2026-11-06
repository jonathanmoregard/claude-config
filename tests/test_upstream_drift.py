"""Contract for scripts/upstream-drift.py, against throwaway local git repos.

Invariants: drift on tracked paths becomes one pending proposal per entry with
collisions on customized paths flagged; a proposal that already records the
newest relevant upstream commit is never rewritten (any status, archived or
not); a reviewed proposal comes back as pending only when upstream moves on a
tracked path; no relevant upstream commit means nothing is written or removed.
No network: fork and upstream are local repos.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
# `runtime/` once the layout migration has landed, the repository root
# before it. Only the projected subset moves; tests/ stays at the root.
RUNTIME = REPO_ROOT / "runtime" if (REPO_ROOT / "runtime").is_dir() else REPO_ROOT
SCRIPT = RUNTIME / "scripts" / "upstream-drift.py"
CONFIG = RUNTIME / "upstream-drift.json"


class Repos:
    """An upstream, a fork cloned from it with one customization, and the
    config + cache + proposals dirs a run of the script needs."""

    def __init__(self, tmp: pathlib.Path):
        self.tmp = tmp
        gitconfig = tmp / "gitconfig"
        gitconfig.write_text(
            "[user]\n\tname = t\n\temail = t@example.invalid\n"
            "[init]\n\tdefaultBranch = main\n"
            # As on the real machine: a bare repo is reachable only by --git-dir.
            "[safe]\n\tbareRepository = explicit\n")
        self.env = {**os.environ, "GIT_CONFIG_GLOBAL": str(gitconfig),
                    "GIT_CONFIG_SYSTEM": "/dev/null"}
        self.upstream = tmp / "upstream"
        self.fork = tmp / "fork"
        self.proposals = tmp / "proposals" / "drift"
        self.git("init", "-q", self.upstream)
        self.commit(self.upstream, "skills/brainstorming/SKILL.md", "v1", "initial")
        self.commit(self.upstream, "hooks/session-start", "v1", "hook")
        self.git("clone", "-q", self.upstream, self.fork)
        self.commit(self.fork, "skills/brainstorming/SKILL.md", "ours", "customize")
        self.config = tmp / "upstream-drift.json"
        self.config.write_text(json.dumps({"entries": [{
            "name": "sp",
            "fork_repo": str(self.fork), "fork_branch": "main",
            "upstream_url": str(self.upstream), "upstream_branch": "main",
            "pin": "vendor/sp",
            "tracked_paths": ["hooks", "skills/brainstorming"],
            "customized_paths": ["skills/brainstorming"],
        }]}))

    def git(self, *args) -> str:
        return subprocess.run(["git", *map(str, args)], check=True, text=True,
                              capture_output=True, env=self.env).stdout.strip()

    def commit(self, repo: pathlib.Path, rel: str, text: str, msg: str) -> str:
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n")
        self.git("-C", repo, "add", "-A")
        self.git("-C", repo, "commit", "-qm", msg)
        return self.git("-C", repo, "rev-parse", "HEAD")

    def run(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--config", str(self.config),
             "--cache-dir", str(self.tmp / "cache"),
             "--proposals-dir", str(self.proposals)],
            capture_output=True, text=True, env=self.env, timeout=60, check=False)

    @property
    def proposal(self) -> pathlib.Path:
        return self.proposals / "sp.md"


def frontmatter(path: pathlib.Path) -> dict[str, str]:
    block = path.read_text().split("---\n")[1]
    return dict(re.findall(r"^(\w+): *(.*)$", block, re.MULTILINE))


def set_status(path: pathlib.Path, status: str) -> None:
    text = path.read_text()
    path.write_text(re.sub(r"^status: .*$", f"status: {status}", text, count=1,
                           flags=re.MULTILINE))


@pytest.fixture
def repos(tmp_path):
    return Repos(tmp_path)


def test_upstream_commits_on_tracked_paths_become_a_pending_proposal(repos):
    collide = repos.commit(repos.upstream, "skills/brainstorming/SKILL.md", "v2", "rework brainstorming")
    hook = repos.commit(repos.upstream, "hooks/session-start", "v2", "faster hook")
    repos.commit(repos.upstream, "README.md", "docs", "readme only")

    r = repos.run()
    assert r.returncode == 0, r.stderr
    fm = frontmatter(repos.proposal)
    assert fm["status"] == "pending"
    assert fm["category"] == "drift"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", fm["date"])
    assert fm["upstream_sha"] == hook, "the newest upstream commit on a tracked path"
    assert fm["fork_sha"] == repos.git("-C", repos.fork, "rev-parse", "HEAD")

    body = repos.proposal.read_text().split("---\n", 2)[2]
    commits, collisions = body.split("## Collisions")[0], body.split("## Collisions")[1]
    assert collide[:12] in commits and hook[:12] in commits
    assert "readme only" not in body, "a commit off the tracked paths is not drift"
    assert "rework brainstorming" in collisions
    assert "faster hook" not in collisions.split("## How to act")[0]
    assert "## How to act" in body


def test_rerun_with_same_upstream_leaves_the_proposal_untouched(repos):
    repos.commit(repos.upstream, "hooks/session-start", "v2", "faster hook")
    assert repos.run().returncode == 0
    before = repos.proposal.read_bytes(), repos.proposal.stat().st_mtime_ns

    repos.commit(repos.upstream, "README.md", "docs", "readme only")
    r = repos.run()
    assert r.returncode == 0, r.stderr
    assert (repos.proposal.read_bytes(), repos.proposal.stat().st_mtime_ns) == before


def test_reviewed_proposal_stays_reviewed_until_upstream_moves(repos):
    repos.commit(repos.upstream, "hooks/session-start", "v2", "faster hook")
    assert repos.run().returncode == 0
    set_status(repos.proposal, "implemented")
    reviewed = repos.proposal.read_bytes()

    assert repos.run().returncode == 0
    assert repos.proposal.read_bytes() == reviewed

    newer = repos.commit(repos.upstream, "skills/brainstorming/SKILL.md", "v3", "newer upstream")
    r = repos.run()
    assert r.returncode == 0, r.stderr
    fm = frontmatter(repos.proposal)
    assert fm["status"] == "pending"
    assert fm["upstream_sha"] == newer


def test_archived_proposal_for_the_same_upstream_is_not_reproposed(repos):
    """Review moves resolved non-rsi proposals to <subdir>/archived/."""
    repos.commit(repos.upstream, "hooks/session-start", "v2", "faster hook")
    assert repos.run().returncode == 0
    set_status(repos.proposal, "implemented")
    archived = repos.proposals / "archived" / "2026-10-15-sp.md"
    archived.parent.mkdir()
    repos.proposal.rename(archived)

    assert repos.run().returncode == 0
    assert not repos.proposal.exists()


def test_no_tracked_upstream_changes_writes_nothing(repos):
    repos.commit(repos.upstream, "README.md", "docs", "readme only")
    r = repos.run()
    assert r.returncode == 0, r.stderr
    assert not repos.proposals.exists()


def test_no_tracked_upstream_changes_removes_nothing(repos):
    repos.proposals.mkdir(parents=True)
    repos.proposal.write_text("---\nstatus: deferred\n---\nkept\n")
    repos.commit(repos.upstream, "README.md", "docs", "readme only")
    assert repos.run().returncode == 0
    assert repos.proposal.read_text() == "---\nstatus: deferred\n---\nkept\n"


def test_unreachable_fork_fails_loudly(repos):
    cfg = json.loads(repos.config.read_text())
    cfg["entries"][0]["fork_repo"] = str(repos.tmp / "missing")
    repos.config.write_text(json.dumps(cfg))
    r = repos.run()
    assert r.returncode != 0
    assert "sp" in r.stderr


def test_every_vendored_skill_link_is_tracked():
    """A skills/ symlink into an entry's pin must be on its tracked paths, or
    upstream changes to that skill would never surface as drift."""
    entries = json.loads(CONFIG.read_text())["entries"]
    for entry in entries:
        # Match on the pin, not on a fixed number of `..` hops. The farm is
        # spelled `../vendor/x` while skills/ sits at the repository root and
        # `../../vendor/x` once it has moved under runtime/, and both name the
        # same submodule -- which is all this assertion is about.
        marker = f"/{entry['pin']}/"
        linked = {os.readlink(p).split(marker, 1)[1]
                  for p in (RUNTIME / "skills").iterdir()
                  if p.is_symlink() and marker in os.readlink(p)}
        assert linked, f"no skills link into {entry['pin']}"
        assert linked <= set(entry["tracked_paths"]), linked - set(entry["tracked_paths"])
        assert set(entry["customized_paths"]) <= set(entry["tracked_paths"])
