"""hooks/completion-check.py: a Stop names the delivery gaps the agent can close.

Real temp git repos (origin = a local bare repo behind a github.com URL via
insteadOf), fixture transcripts, a stub `gh` first on PATH, a throwaway HOME.
No network.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HOOK = ROOT / "hooks/completion-check.py"
NWO = "acme/widget"
URL = f"https://github.com/{NWO}.git"
GENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null"}

STUB = r"""#!/usr/bin/env python3
import json, pathlib, sys
here = pathlib.Path(__file__).parent
cfg = json.loads((here / "gh.json").read_text())
a = sys.argv[1:]
with open(here / "gh.log", "a") as f:
    f.write(" ".join(a) + "\n")
if cfg.get("fail"):
    print("HTTP 401: Bad credentials", file=sys.stderr); sys.exit(1)
def opt(n):
    return a[a.index(n) + 1] if n in a else None
if a[:2] == ["pr", "list"]:
    print(json.dumps(cfg.get("prs", {}).get(opt("--head"), [])))
elif a[:2] == ["repo", "view"]:
    print(json.dumps({"visibility": cfg.get("visibility", "PUBLIC"),
                      "defaultBranchRef": {"name": "main"}}))
else:
    sys.exit(2)
"""


def load_module():
    spec = importlib.util.spec_from_file_location("completion_check", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sh(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True, env={**os.environ, **GENV}).stdout.strip()


class World:
    def __init__(self, tmp: pathlib.Path):
        self.tmp = tmp
        self.home = tmp / "home"
        self.home.mkdir()
        self.bin = tmp / "bin"
        self.bin.mkdir()
        (self.bin / "gh").write_text(STUB)
        (self.bin / "gh").chmod(0o755)
        self.gh()
        self.entries: list[dict] = []
        self.transcript = tmp / "proj" / "sess-1.jsonl"
        self.transcript.parent.mkdir()

    def gh(self, **cfg):
        (self.bin / "gh.json").write_text(json.dumps(cfg))

    def gh_calls(self):
        log = self.bin / "gh.log"
        return log.read_text().splitlines() if log.exists() else []

    def repo(self, name="widget", origin=True, under=None) -> pathlib.Path:
        work = (under or self.tmp) / name
        subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
        sh(work, "commit", "-q", "--allow-empty", "-m", "init")
        if origin:
            bare = self.tmp / f"{name}-origin.git"
            subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
            sh(work, "config", f"url.{bare}.insteadOf", URL)
            sh(work, "remote", "add", "origin", URL)
            sh(work, "push", "-q", "-u", "origin", "main")
            sh(work, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        return work

    def feature(self, work, branch="feat/x", push=True):
        sh(work, "switch", "-q", "-c", branch)
        (work / "f.txt").write_text("x\n")
        sh(work, "add", "f.txt")
        sh(work, "commit", "-q", "-m", "work")
        if push:
            sh(work, "push", "-q", "-u", "origin", branch)

    def bash(self, command, cwd, transcript=None):
        entry = {"type": "assistant", "cwd": str(cwd), "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": command}}]}}
        path = transcript or self.transcript
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def run(self, **payload):
        if not self.transcript.exists():
            self.transcript.write_text("")
        stdin = json.dumps({"session_id": "sess-1", "hook_event_name": "Stop",
                            "transcript_path": str(self.transcript), **payload})
        p = subprocess.run([sys.executable, str(HOOK)], input=stdin, capture_output=True,
                           text=True, timeout=60,
                           env={"PATH": f"{self.bin}:{os.environ['PATH']}", "HOME": str(self.home),
                                "GIT_CONFIG_GLOBAL": "/dev/null"})
        assert p.returncode == 0, p.stderr
        return json.loads(p.stdout) if p.stdout.strip() else None


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


def pr(state, checks=()):
    return {"number": 7, "url": f"https://github.com/{NWO}/pull/7", "state": state,
            "statusCheckRollup": [{"name": n, "status": "COMPLETED" if c else "IN_PROGRESS",
                                   "conclusion": c} for n, c in checks]}


# ------------------------------------------------------------ gaps that block

def test_branch_never_pushed_blocks(w):
    repo = w.repo()
    w.feature(repo, push=False)
    w.bash("git commit -m work", repo)
    out = w.run()
    assert out["decision"] == "block"
    assert "never pushed" in out["reason"] and "push -u origin feat/x" in out["reason"]


def test_unpushed_commit_on_pushed_branch_blocks(w):
    repo = w.repo()
    w.feature(repo)
    sh(repo, "commit", "-q", "--allow-empty", "-m", "more")
    w.bash(f"git -C {repo} commit -m more", w.home)
    out = w.run()
    assert "1 commit(s) on feat/x not pushed" in out["reason"]


def test_pushed_branch_without_pr_in_public_repo_blocks(w):
    repo = w.repo()
    w.feature(repo)
    w.bash("git push -u origin feat/x", repo)
    w.gh(visibility="PUBLIC")
    out = w.run()
    assert out["decision"] == "block"
    assert f"gh pr create -R {NWO} --head feat/x --fill" in out["reason"]


def test_private_repo_without_pr_ci_still_needs_a_pr(w):
    repo = w.repo()
    w.feature(repo)
    w.bash("git push", repo)
    w.gh(visibility="PRIVATE")
    assert "has no PR" in w.run()["reason"]


def test_open_pr_with_failed_check_blocks(w):
    repo = w.repo()
    w.feature(repo)
    w.bash("gh pr create --fill", repo)
    w.gh(prs={"feat/x": [pr("OPEN", [("lint", "SUCCESS"), ("tests", "FAILURE")])]})
    out = w.run()
    assert "failing checks: tests" in out["reason"] and "gh pr checks 7" in out["reason"]


def test_merged_pr_with_primary_still_on_feature_branch_blocks(w):
    repo = w.repo()
    w.feature(repo)
    w.bash("git push", repo)
    w.gh(prs={"feat/x": [pr("MERGED")]})
    out = w.run()
    assert out["decision"] == "block"
    assert "switch main" in out["reason"] and "pull --ff-only" in out["reason"]


def test_merged_pr_after_remote_branch_deleted_still_says_switch(w):
    """A deleted remote branch leaves `@{u}` dangling; that is pushed, not 'never pushed'."""
    repo = w.repo()
    w.feature(repo)
    sh(repo, "push", "-q", "origin", "--delete", "feat/x")
    sh(repo, "fetch", "-q", "--prune")
    w.bash("git push", repo)
    w.gh(prs={"feat/x": [pr("MERGED")]})
    reason = w.run()["reason"]
    assert "switch main" in reason and "never pushed" not in reason


def test_primary_on_default_behind_origin_blocks(w):
    repo = w.repo()
    w.bash("git push", repo)
    other = w.tmp / "other"
    subprocess.run(["git", "clone", "-q", str(w.tmp / "widget-origin.git"), str(other)], check=True)
    sh(other, "commit", "-q", "--allow-empty", "-m", "someone merged")
    sh(other, "push", "-q", "origin", "main")
    out = w.run()
    assert "behind origin" in out["reason"] and "pull --ff-only" in out["reason"]


def test_dirty_worktree_created_by_session_blocks(w):
    repo = w.repo()
    wt = w.tmp / "wt"
    sh(repo, "worktree", "add", "-q", "-b", "feat/y", str(wt))
    (wt / "scratch.txt").write_text("wip\n")
    w.bash(f"git worktree add -b feat/y {wt}", repo)
    out = w.run()
    assert "uncommitted changes in a worktree this session created" in out["reason"]


# ------------------------------------------------------------ never a gap

def test_open_green_pr_awaiting_merge_click_passes(w):
    repo = w.repo()
    w.feature(repo)
    w.bash("gh pr create --fill", repo)
    w.gh(prs={"feat/x": [pr("OPEN", [("tests", "SUCCESS")])]})
    assert w.run() is None


def test_pending_checks_pass(w):
    repo = w.repo()
    w.feature(repo)
    w.bash("git push", repo)
    w.gh(prs={"feat/x": [pr("OPEN", [("tests", None)])]})
    assert w.run() is None


def test_pr_gated_repo_without_pr_passes(w):
    repo = w.repo()
    w.feature(repo)
    (repo / ".github/workflows").mkdir(parents=True)
    (repo / ".github/workflows/ci.yml").write_text("on:\n  pull_request:\n  push:\n")
    w.bash("git push", repo)
    w.gh(visibility="PRIVATE")
    assert w.run() is None


def test_merged_pr_in_linked_worktree_passes(w):
    repo = w.repo()
    wt = w.tmp / "wt"
    sh(repo, "worktree", "add", "-q", "-b", "feat/x", str(wt))
    sh(wt, "commit", "-q", "--allow-empty", "-m", "work")
    sh(wt, "push", "-q", "-u", "origin", "feat/x")
    w.bash("git push", wt)
    w.gh(prs={"feat/x": [pr("MERGED")]})
    assert w.run() is None


def _advance_origin(w):
    other = w.tmp / "other"
    subprocess.run(["git", "clone", "-q", str(w.tmp / "widget-origin.git"), str(other)], check=True)
    sh(other, "commit", "-q", "--allow-empty", "-m", "someone merged")
    sh(other, "push", "-q", "origin", "main")


def test_dirty_primary_checkout_is_not_a_gap(w):
    """Uncommitted changes in a primary checkout may be the user's: no pull demanded."""
    repo = w.repo()
    (repo / "user-edit.txt").write_text("theirs\n")
    w.bash("git push", repo)
    _advance_origin(w)
    assert w.run() is None


def test_linked_worktree_on_default_behind_origin_is_not_a_gap(w):
    repo = w.repo()
    wt = w.tmp / "wt-main"
    sh(repo, "switch", "-q", "-c", "parking")
    sh(repo, "worktree", "add", "-q", str(wt), "main")
    w.bash("git push", wt)
    _advance_origin(w)
    assert w.run() is None


def test_pinned_claude_primary_is_excluded(w):
    claude = w.repo(name=".claude", under=w.home)
    sh(claude, "commit", "-q", "--allow-empty", "-m", "unpushed on master")
    w.bash("git commit -m x", claude)
    assert w.run() is None


def test_repo_without_origin_is_scratch(w):
    repo = w.repo(origin=False)
    w.bash("git commit -m x", repo)
    assert w.run() is None


def test_no_git_activity_makes_no_calls(w):
    w.bash("ls -la", w.tmp)
    assert w.run() is None
    assert w.gh_calls() == []


# ------------------------------------------------------------ loop guard, fail open

def test_stop_hook_active_gives_notice_not_block(w):
    repo = w.repo()
    w.feature(repo, push=False)
    w.bash("git commit -m work", repo)
    out = w.run(stop_hook_active=True)
    assert "decision" not in out and "never pushed" in out["systemMessage"]


def test_background_work_in_flight_gives_notice_not_block(w):
    repo = w.repo()
    w.feature(repo, push=False)
    w.bash("git commit -m work", repo)
    out = w.run(background_tasks=[{"id": "b1"}])
    assert "decision" not in out and "never pushed" in out["systemMessage"]


def test_gh_failure_fails_open_with_notice(w):
    repo = w.repo()
    w.feature(repo)
    w.bash("git push", repo)
    w.gh(fail=True)
    out = w.run()
    assert "decision" not in out and "could not measure" in out["systemMessage"]


@pytest.mark.parametrize("raw", ["", "nope", "[]", '{"transcript_path": 3}'])
def test_unparseable_input_is_silent(w, raw):
    p = subprocess.run([sys.executable, str(HOOK)], input=raw, capture_output=True, text=True)
    assert p.returncode == 0 and p.stdout == ""


# ------------------------------------------------------------ what counts as touched

def test_subagent_transcript_counts(w):
    repo = w.repo()
    w.feature(repo, push=False)
    w.transcript.write_text("")
    sub = w.transcript.parent / "sess-1" / "subagents"
    sub.mkdir(parents=True)
    w.bash("git commit -m work", repo, transcript=sub / "agent-a1.jsonl")
    assert "never pushed" in w.run()["reason"]


def test_git_broker_mcp_push_counts(w):
    repo = w.repo()
    w.feature(repo)
    w.gh(visibility="PUBLIC")
    entry = {"type": "assistant", "cwd": "/", "message": {"content": [{
        "type": "tool_use", "name": "mcp__git-broker__git",
        "input": {"repo_path": str(repo), "operation": "push", "branch": "feat/x"}}]}}
    w.transcript.write_text(json.dumps(entry) + "\n")
    assert "has no PR" in w.run()["reason"]


@pytest.mark.parametrize("command,cwd,dirs,created", [
    ("git commit -m x", "/r", ["/r"], []),
    ("cd /a && git push", "/r", ["/a"], []),
    ("cd ~/w; git -C sub commit -qm 'a && b'", "/r", ["/h/w/sub"], []),
    ("FOO=1 timeout 30 git push origin x", "/r", ["/r"], []),
    ("git -C /r worktree add -b feat/z ../wt origin/main", "/x", ["/r", "/wt"], ["/wt"]),
    ("bash $HOME/.claude/scripts/claude-worktree.sh my-slug", "/r",
     ["/h/worktrees/claude-my-slug"], ["/h/worktrees/claude-my-slug"]),
    ("gh pr create --fill", "/r", ["/r"], []),
    ("git status && git log && gh pr view 3", "/r", [], []),
    ('echo "git push" > notes.txt', "/r", [], []),
])
def test_scan_bash(command, cwd, dirs, created):
    mod = load_module()
    t = mod.Touched([], set())
    mod.scan_bash(command, cwd, "/h", t)
    assert t.dirs == dirs and sorted(t.created) == created
