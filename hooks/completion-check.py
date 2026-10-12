#!/usr/bin/env python3
"""Stop hook: name the delivery gaps this session left that it can close itself.

Definition of done (Jonathan, 2026-08-27): move as close to finished as
possible; the only things left for him are bare necessities (the merge click,
credentials, interactive logins, taste calls). A session ends on the default
branch, clean, at latest.

Stateless. Every Stop re-derives everything:

1. WHAT THIS SESSION TOUCHED comes from its transcript (`transcript_path`)
   plus its subagents' transcripts (`<transcript dir>/<session>/subagents/
   agent-*.jsonl`; a glob, so cheap, and subagents are where delegated git work
   happens). A checkout counts when a tool call ran, in it, one of:
   `git commit`, `git push`, `git worktree add`, `gh pr create`,
   `scripts/claude-worktree.sh` / `scripts/klaffat-worktree.sh` (which create
   `~/worktrees/{claude,klaffat}-<slug>`), or the git-broker MCP operations
   commit / push / worktree_add / pr_create. The directory comes from
   `git -C`, a preceding `cd`, or the entry's `cwd`. Worktrees created by
   `git worktree add`, the two helper scripts or the broker are "created by
   this session".

2. WHAT IS STILL OPEN is measured live with git and gh, per checkout, in
   parallel (at most MAX_CHECKOUTS, most recently touched first):
   - commits not pushed (ahead of upstream, or a branch never pushed);
   - a pushed feature branch with commits and no PR, in a repo that is not
     PR-gated (private + a workflow on `pull_request`: there the PR waits for
     the user's "Open the PR?" answer, which is not a gap);
   - an open PR with a failed check;
   - a merged PR whose PRIMARY checkout still sits on the feature branch, or a
     primary checkout on the default branch that is behind origin
     (`git ls-remote`, no fetch); linked worktrees may stay until cleanup;
   - uncommitted changes in a worktree this session created.

Never a gap: an open, green PR awaiting the merge click; checks still pending;
a PR-gated repo with no PR; a closed PR; a repo with no `origin` remote
(scratch); the `~/.claude` primary checkout (pinned live config; its dirty
`settings.json` is the user's); a dirty primary checkout.

Output: gaps on the first Stop of a chain -> one `decision: block` listing each
gap with its next command. With `stop_hook_active` set, or background work in
flight (`background_tasks` / `session_crons` non-empty: the turn is pausing on a
wait, not finishing) -> a `systemMessage` only. A checkout that git or gh could
not measure -> named in a `systemMessage`, never a block. Unparseable input ->
silent exit 0.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import NamedTuple

GIT_TIMEOUT_S = 5
GH_TIMEOUT_S = 8
MAX_CHECKOUTS = 8
FAILED = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}
GITHUB_RE = re.compile(r"github\.com[:/]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")
SEPARATORS = {"&&", "||", ";", "|", "&", "(", ")", ";;", "|&"}
WRAPPERS = {"command", "env", "exec", "nice", "nohup"}
HELPERS = {"claude-worktree.sh": "claude-", "klaffat-worktree.sh": "klaffat-"}
BROKER_TOOL = "mcp__git-broker__git"


class Touched(NamedTuple):
    dirs: list[str]          # checkout dirs, oldest touch first, may repeat
    created: set[str]        # worktree paths this session created


class Unverified(Exception):
    pass


# ---------------------------------------------------------------- transcript

def _resolve(path: str, base: str, home: str) -> str:
    path = path.replace("${HOME}", home).replace("$HOME", home)
    if path == "~" or path.startswith("~/"):
        path = home + path[1:]
    return os.path.normpath(os.path.join(base, path))


def _tokens(command: str) -> list[str]:
    lex = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        return list(lex)
    except ValueError:
        return command.split()


def _simple_commands(command: str) -> list[list[str]]:
    out, cur = [], []
    for tok in _tokens(command):
        if tok in SEPARATORS:
            if cur:
                out.append(cur)
            cur = []
        else:
            cur.append(tok)
    if cur:
        out.append(cur)
    return out


def _strip_prefix(words: list[str]) -> list[str]:
    i = 0
    while i < len(words):
        w = words[i]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w) or w in WRAPPERS:
            i += 1
        elif w == "timeout" and i + 1 < len(words):
            i += 2
        else:
            break
    return words[i:]


def _git_target(words: list[str], cwd: str, home: str):
    """(dir, subcommand, rest) for a `git ...` word list."""
    i, d = 1, cwd
    while i < len(words) and words[i].startswith("-"):
        if words[i] == "-C" and i + 1 < len(words):
            d = _resolve(words[i + 1], d, home)
            i += 2
        elif words[i] in ("-c", "--git-dir", "--work-tree", "--namespace"):
            i += 2
        else:
            i += 1
    if i >= len(words):
        return d, "", []
    return d, words[i], words[i + 1:]


def _worktree_add_path(rest: list[str]) -> str | None:
    if not rest or rest[0] != "add":
        return None
    i = 1
    while i < len(rest):
        w = rest[i]
        if w == "--":
            return rest[i + 1] if i + 1 < len(rest) else None
        if w in ("-b", "-B", "--reason"):
            i += 2
        elif w.startswith("-"):
            i += 1
        else:
            return w
    return None


def scan_bash(command: str, cwd: str, home: str, t: Touched) -> None:
    cur = cwd
    for words in _simple_commands(command):
        words = _strip_prefix(words)
        if not words:
            continue
        head = words[0]
        if head in ("cd", "pushd"):
            target = words[1] if len(words) > 1 else home
            if target != "-":
                cur = _resolve(target, cur, home)
        elif head == "git":
            d, sub, rest = _git_target(words, cur, home)
            if sub in ("commit", "push"):
                t.dirs.append(d)
            elif sub == "worktree":
                path = _worktree_add_path(rest)
                if path:
                    t.dirs.append(d)
                    new = _resolve(path, d, home)
                    t.created.add(new)
                    t.dirs.append(new)
        elif head == "gh" and words[1:3] == ["pr", "create"]:
            t.dirs.append(cur)
        else:
            for w in words[:2]:
                prefix = HELPERS.get(os.path.basename(w))
                if prefix:
                    rest = words[words.index(w) + 1:]
                    if rest and not rest[0].startswith("-"):
                        new = os.path.join(home, "worktrees", prefix + rest[0])
                        t.created.add(new)
                        t.dirs.append(new)
                    break


def scan_entry(entry: dict, home: str, t: Touched) -> None:
    msg = entry.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(content, list):
        return
    cwd = entry.get("cwd") if isinstance(entry.get("cwd"), str) else home
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "tool_use":
            continue
        inp = item.get("input") if isinstance(item.get("input"), dict) else {}
        if item.get("name") == "Bash" and isinstance(inp.get("command"), str):
            scan_bash(inp["command"], cwd, home, t)
        elif item.get("name") == BROKER_TOOL and isinstance(inp.get("repo_path"), str):
            op, repo = inp.get("operation"), inp["repo_path"]
            if op in ("commit", "push", "pr_create", "worktree_add"):
                t.dirs.append(repo)
            if op == "worktree_add" and isinstance(inp.get("worktree_path"), str):
                new = _resolve(inp["worktree_path"], repo, home)
                t.created.add(new)
                t.dirs.append(new)


def transcripts_for(transcript_path: str, session_id: str) -> list[pathlib.Path]:
    main = pathlib.Path(transcript_path)
    subs = sorted((main.parent / session_id / "subagents").glob("agent-*.jsonl")) \
        if re.match(r"^[A-Za-z0-9._-]+$", session_id or "") else []
    return [main, *subs]


def touched_by(paths: list[pathlib.Path], home: str) -> Touched:
    t = Touched([], set())
    for p in paths:
        try:
            fh = p.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"tool_use"' not in line or not (
                        "git" in line or "gh pr create" in line or "worktree" in line):
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict):
                    scan_entry(entry, home, t)
    return t


# ---------------------------------------------------------------- live state

def run(argv: list[str], timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Unverified(f"{argv[0]} {' '.join(argv[1:3])}: {type(exc).__name__}")


def git(top: str, *args: str, ok_fail: bool = False) -> str | None:
    # Read-only: never take optional locks (index refresh) in a checkout.
    p = run(["git", "--no-optional-locks", "-C", top, *args], GIT_TIMEOUT_S)
    if p.returncode != 0:
        if ok_fail:
            return None
        raise Unverified(f"git {args[0]}: {(p.stderr or '').strip()[:120]}")
    return p.stdout.strip()


def gh_json(gh: str, *args: str):
    p = run([gh, *args], GH_TIMEOUT_S)
    if p.returncode != 0:
        raise Unverified(f"gh {' '.join(args[:2])}: {(p.stderr or '').strip()[:120]}")
    try:
        return json.loads(p.stdout)
    except ValueError:
        raise Unverified(f"gh {' '.join(args[:2])}: unparseable output")


def toplevel(d: str) -> str | None:
    if not os.path.isdir(d):
        return None
    p = run(["git", "--no-optional-locks", "-C", d, "rev-parse", "--show-toplevel"], GIT_TIMEOUT_S)
    return os.path.realpath(p.stdout.strip()) if p.returncode == 0 else None


def pr_gated(top: str, gh: str, nwo: str) -> bool:
    vis = gh_json(gh, "repo", "view", nwo, "--json", "visibility").get("visibility")
    if vis == "PUBLIC":
        return False
    wf = pathlib.Path(top, ".github", "workflows")
    for f in list(wf.glob("*.yml")) + list(wf.glob("*.yaml")):
        try:
            if re.search(r"^\s*pull_request\b|[\[{,\s]pull_request\b", f.read_text(), re.M):
                return True
        except OSError:
            continue
    return False


def inspect(top: str, created: bool, gh: str | None) -> list[tuple[str, str]]:
    """[(gap, next command)] for one checkout. Raises Unverified."""
    gaps: list[tuple[str, str]] = []
    primary = os.path.realpath(git(top, "rev-parse", "--absolute-git-dir")) == \
        os.path.realpath(git(top, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    url = git(top, "config", "--get", "remote.origin.url", ok_fail=True)
    if not url:
        return []
    m = GITHUB_RE.search(url)
    nwo = f"{m.group(1)}/{m.group(2)}" if m else None
    branch = git(top, "branch", "--show-current")
    if not branch:
        return []
    head = git(top, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", ok_fail=True)
    default = head.split("/", 1)[1] if head else None
    if not default and nwo and gh:
        default = gh_json(gh, "repo", "view", nwo, "--json", "defaultBranchRef")["defaultBranchRef"]["name"]
    default = default or "main"
    dirty = bool(git(top, "status", "--porcelain"))
    q = shlex.quote(top)

    if created and dirty:
        gaps.append(("uncommitted changes in a worktree this session created",
                     f"git -C {q} status  (commit them, or discard scratch)"))

    # Upstream: the configured one, else origin/<branch>. A configured upstream
    # whose ref is gone (remote branch deleted after merge) counts as pushed.
    if git(top, "rev-parse", "--verify", "--quiet", "@{u}", ok_fail=True):
        upstream = "@{u}"
    elif git(top, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}", ok_fail=True):
        upstream = f"refs/remotes/origin/{branch}"
    else:
        upstream = None
    gone = upstream is None and git(top, "config", "--get", f"branch.{branch}.merge", ok_fail=True)
    if upstream:
        ahead = int(git(top, "rev-list", "--count", f"{upstream}..HEAD") or 0)
        if ahead:
            gaps.append((f"{ahead} commit(s) on {branch} not pushed", f"git -C {q} push"))
            return gaps
    elif branch != default and not gone:
        base = git(top, "rev-parse", "--verify", "--quiet", f"origin/{default}", ok_fail=True)
        ahead = int(git(top, "rev-list", "--count", f"origin/{default}..HEAD") or 0) if base else 1
        if ahead:
            gaps.append((f"branch {branch} was never pushed",
                         f"git -C {q} push -u origin {shlex.quote(branch)}"))
        return gaps

    if branch == default:
        if primary and not dirty:
            line = git(top, "ls-remote", "origin", f"refs/heads/{default}")
            remote = line.split()[0] if line else ""
            local = git(top, "rev-parse", "HEAD")
            if remote and remote != local:
                known = git(top, "cat-file", "-e", f"{remote}^{{commit}}", ok_fail=True) is not None
                behind = not known or git(top, "merge-base", "--is-ancestor", remote, "HEAD",
                                          ok_fail=True) is None
                if behind:
                    gaps.append((f"primary checkout on {default} is behind origin",
                                 f"git -C {q} pull --ff-only"))
        return gaps

    if not nwo:
        return gaps
    if not gh:
        raise Unverified("gh is not on PATH")
    prs = gh_json(gh, "pr", "list", "-R", nwo, "--head", branch, "--state", "all", "--limit", "5",
                  "--json", "number,url,state,statusCheckRollup")
    if not prs:
        has_commits = int(git(top, "rev-list", "--count", f"origin/{default}..HEAD") or 0)
        if has_commits and not pr_gated(top, gh, nwo):
            gaps.append((f"{branch} is pushed but has no PR",
                         f"gh pr create -R {nwo} --head {shlex.quote(branch)} --fill"))
        return gaps
    pr = max(prs, key=lambda p: p.get("number", 0))
    if pr.get("state") == "OPEN":
        failed = sorted({c.get("name") or c.get("context") or "?"
                         for c in pr.get("statusCheckRollup") or []
                         if (c.get("conclusion") or c.get("state") or "").upper() in FAILED})
        if failed:
            gaps.append((f"PR {pr['url']} has failing checks: {', '.join(failed)[:120]}",
                         f"gh pr checks {pr['number']} -R {nwo}"))
    elif pr.get("state") == "MERGED" and primary:
        gaps.append((f"PR {pr['url']} merged; this primary checkout is still on {branch}",
                     f"git -C {q} switch {shlex.quote(default)} && git -C {q} pull --ff-only"))
    return gaps


# ---------------------------------------------------------------- decision

def checkouts(t: Touched, home: str) -> list[str]:
    """Distinct toplevels, most recently touched first, ~/.claude primary excluded."""
    pinned = os.path.realpath(os.path.join(home, ".claude"))
    seen: list[str] = []
    for d in reversed(t.dirs):
        top = toplevel(d)
        if top and top != pinned and top not in seen:
            seen.append(top)
        if len(seen) >= MAX_CHECKOUTS:
            break
    return seen


def decide(payload: dict, home: str, gh: str | None) -> dict | None:
    tp, sid = payload.get("transcript_path"), payload.get("session_id")
    if not isinstance(tp, str) or not tp:
        return None
    t = touched_by(transcripts_for(tp, str(sid or "")), home)
    if not t.dirs:
        return None
    tops = checkouts(t, home)
    if not tops:
        return None
    created = {os.path.realpath(c) for c in t.created}

    def one(top):
        try:
            return top, inspect(top, top in created, gh), None
        except Unverified as exc:
            return top, [], str(exc)

    with ThreadPoolExecutor(max_workers=len(tops)) as pool:
        results = list(pool.map(one, tops))

    gaps = [(top, g, c) for top, found, _ in results for g, c in found]
    unverified = [f"{top} ({err})" for top, _, err in results if err]
    notice = ("Completion check: could not measure " + "; ".join(unverified)) if unverified else None
    if not gaps:
        return {"systemMessage": notice} if notice else None

    lines = [f"{i}. {top}: {g}\n   next: {c}" for i, (top, g, c) in enumerate(gaps, 1)]
    facts = ("Completion check: work this session touched is not finished:\n" + "\n".join(lines))
    pausing = payload.get("background_tasks") or payload.get("session_crons")
    if payload.get("stop_hook_active") or pausing:
        return {"systemMessage": facts + (("\n" + notice) if notice else "")}
    out: dict = {"decision": "block", "reason": facts + (
        "\nDone means pushed, PR open with green checks (the merge click is the user's), "
        "and after a merge the primary checkout back on the default branch at latest. "
        "Close these gaps, or state in the reply why one must stay open (for example the "
        "user asked to hold it).")}
    if notice:
        out["systemMessage"] = notice
    return out


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        out = decide(payload, str(pathlib.Path.home()), shutil.which("gh"))
    except Exception:  # a checker that crashes must not wedge the turn
        return 0
    if out:
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
