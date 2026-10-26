#!/usr/bin/env python3
"""UserPromptSubmit hook: tell the agent which delivery gaps it can still close.

Definition of done (Jonathan, 2026-08-27): move as close to finished as
possible; the only things left for him are bare necessities (the merge click,
credentials, interactive logins, taste calls). A session ends on the default
branch, clean, at latest.

Informs, never forces (Jonathan, 2026-10-25: "I distinctly recall asking for it
to be non-forcing, but rather have the agent be informed of status"). The gaps
arrive as `additionalContext` at the start of a turn — when the agent can act on
them — and no output of this hook can block a Stop or end a turn.

Stateless. Every prompt re-derives everything:

1. WHAT THIS SESSION TOUCHED comes from its transcript (`transcript_path`)
   plus its subagents' transcripts (`<transcript dir>/<session>/subagents/
   agent-*.jsonl`; a glob, so cheap, and subagents are where delegated git work
   happens). A checkout counts when a tool call SUCCEEDED (its tool_result
   arrived without is_error) after running, in it, one of:
   `git commit`, `git push`, `git worktree add`, `gh pr create`,
   `scripts/claude-worktree.sh` / `scripts/klaffat-worktree.sh` (which create
   `~/worktrees/{claude,klaffat}-<slug>`), or the git-broker MCP operations
   commit / push / worktree_add / pr_create. The directory comes from
   `git -C`, a preceding `cd`, or the entry's `cwd`; a worktree add claims
   only the new worktree, never the checkout it was run from (whose current
   branch may be another session's). Worktrees created by
   `git worktree add`, the two helper scripts or the broker are "created by
   this session" only when, in addition, the output shows no creation failure
   ("already exists", `fatal:`): a failed add never claims someone's worktree.

2. WHAT IS STILL OPEN is measured live with git and gh, per checkout, in
   parallel (at most MAX_CHECKOUTS, most recently touched first). This runs on
   the prompt's critical path, so the per-call timeouts stay small and the
   hook's own `timeout` in settings.json is the hard ceiling: killed, it says
   nothing rather than delaying the turn. What counts as open:
   - commits not pushed: ahead of the branch's PUSH destination (`@{push}`,
     else push remote + same branch name; never an inherited `@{u}`), or a
     branch with no remote ref and no PR (never pushed);
   - a pushed feature branch with commits and no PR, in a repo that is not
     PR-gated (non-public + a workflow whose `on:` includes `pull_request` or
     `pull_request_target`, read with yq: there the PR waits for the user's
     "Open the PR?" answer, which is not a gap);
   - an open PR with a failed check;
   - a merged PR whose clean PRIMARY checkout still sits on the branch, or a
     clean primary checkout on the default branch that is behind origin
     (`git ls-remote`, no fetch); linked worktrees may stay until cleanup;
   - uncommitted changes in a worktree this session created.

Never a gap: an open, green PR awaiting the merge click; checks still pending;
a PR-gated repo with no PR; a closed PR; a repo with no `origin` remote
(scratch); the `~/.claude` primary checkout (pinned live config; its dirty
`settings.json` is the user's); a dirty primary checkout.

Output: one `additionalContext` naming each gap with the command that closes it,
and naming any checkout git or gh could not measure (so an unmeasured checkout
does not read as "all clear"). Nothing to say, or unparseable input -> silent
exit 0. There is no blocking path: no `decision`, no `continue: false`.
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
PR_EVENTS = {"pull_request", "pull_request_target"}
FAILED = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}
GITHUB_RE = re.compile(r"github\.com[:/]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")
SEPARATORS = {"&&", "||", ";", "|", "&", "(", ")", ";;", "|&"}
WRAPPERS = {"command", "env", "exec", "nice", "nohup"}
HELPERS = {"claude-worktree.sh": "claude-", "klaffat-worktree.sh": "klaffat-"}
BROKER_TOOL = "mcp__git-broker__git"
TOOL_RESULT_ID_RE = re.compile(r'"tool_use_id":\s*"([^"]+)"')
CREATE_FAILED_RE = re.compile(r"already exists|^fatal:|^error:", re.M | re.I)


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
                    # Only the new worktree is this session's: the anchor
                    # checkout's current branch may be anyone's.
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


def scan_entry(entry: dict, home: str) -> list[tuple[str, Touched]]:
    """[(tool_use id, what that call would touch)] for one transcript entry."""
    msg = entry.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(content, list):
        return []
    cwd = entry.get("cwd") if isinstance(entry.get("cwd"), str) else home
    out = []
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "tool_use" \
                or not isinstance(item.get("id"), str):
            continue
        t = Touched([], set())
        inp = item.get("input") if isinstance(item.get("input"), dict) else {}
        if item.get("name") == "Bash" and isinstance(inp.get("command"), str):
            scan_bash(inp["command"], cwd, home, t)
        elif item.get("name") == BROKER_TOOL and isinstance(inp.get("repo_path"), str):
            op, repo = inp.get("operation"), inp["repo_path"]
            if op in ("commit", "push", "pr_create"):
                t.dirs.append(repo)
            if op == "worktree_add" and isinstance(inp.get("worktree_path"), str):
                new = _resolve(inp["worktree_path"], repo, home)
                t.created.add(new)
                t.dirs.append(new)
        if t.dirs:
            out.append((item["id"], t))
    return out


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
    return ""


def transcripts_for(transcript_path: str, session_id: str) -> list[pathlib.Path]:
    main = pathlib.Path(transcript_path)
    subs = sorted((main.parent / session_id / "subagents").glob("agent-*.jsonl")) \
        if re.match(r"^[A-Za-z0-9._-]+$", session_id or "") else []
    return [main, *subs]


def touched_by(paths: list[pathlib.Path], home: str) -> Touched:
    """Checkouts touched by tool calls that SUCCEEDED.

    A tool_use counts only once its tool_result arrives without is_error: a
    call refused by a hook or permission rule, a failing command, or one with
    no result yet claims nothing. A worktree counts as created by this session
    only when, in addition, the output carries no creation failure (a
    `…; echo done` compound exits 0 after `fatal: '<path>' already exists`).
    """
    t = Touched([], set())
    pending: dict[str, Touched] = {}
    for p in paths:
        try:
            fh = p.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"tool_use"' in line and (
                        "git" in line or "gh pr create" in line or "worktree" in line):
                    entry = _loads(line)
                    if entry:
                        pending.update(scan_entry(entry, home))
                elif '"tool_result"' in line and pending and any(
                        i in pending for i in TOOL_RESULT_ID_RE.findall(line)):
                    entry = _loads(line)
                    content = (entry or {}).get("message", {}).get("content")
                    for item in content if isinstance(content, list) else []:
                        if not isinstance(item, dict) or item.get("type") != "tool_result":
                            continue
                        claim = pending.pop(item.get("tool_use_id"), None)
                        if claim is None or item.get("is_error"):
                            continue
                        t.dirs.extend(claim.dirs)
                        if not CREATE_FAILED_RE.search(_result_text(item.get("content"))):
                            t.created.update(claim.created)
    return t


def _loads(line: str) -> dict | None:
    try:
        entry = json.loads(line)
    except ValueError:
        return None
    return entry if isinstance(entry, dict) else None


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


def workflow_events(yq: str | None, path: pathlib.Path) -> set[str]:
    """Event names under a workflow's `on:` (scalar, list or mapping, any quoting).

    Parsed by yq-go (declared system-wide in nixos-config, YAML 1.2, so `on` is
    a plain key, not the YAML 1.1 boolean). A hand-rolled matcher missed quoted
    and flow forms; the stdlib has no YAML parser. Raises Unverified when yq is
    missing or the file does not parse.
    """
    if not yq:
        raise Unverified("yq is not on PATH; cannot tell whether the repo is PR-gated")
    p = run([yq, "-o", "json", ".on", str(path)], GIT_TIMEOUT_S)
    if p.returncode != 0:
        raise Unverified(f"yq {path.name}: {(p.stderr or '').strip()[:120]}")
    try:
        on = json.loads(p.stdout or "null")
    except ValueError:
        raise Unverified(f"yq {path.name}: unparseable output")
    if isinstance(on, str):
        return {on}
    if isinstance(on, list):
        return {e for e in on if isinstance(e, str)}
    if isinstance(on, dict):
        return set(on)
    return set()


def pr_gated(top: str, gh: str, nwo: str, yq: str | None) -> bool:
    """CLAUDE.md: non-public repo whose workflows run on pull_request."""
    vis = gh_json(gh, "repo", "view", nwo, "--json", "visibility").get("visibility")
    if vis == "PUBLIC":
        return False
    wf = pathlib.Path(top, ".github", "workflows")
    files = sorted(list(wf.glob("*.yml")) + list(wf.glob("*.yaml")))
    return any(workflow_events(yq, f) & PR_EVENTS for f in files)


def push_target(top: str, branch: str) -> tuple[str, str, str]:
    """(remote, destination branch, remote-tracking ref) a push of `branch` goes to.

    Git's own answer, `@{push}`, when it resolves. It does not under
    push.default=simple when the upstream was inherited from another branch
    (e.g. a worktree cut from origin/main); then the push remote
    (branch.<b>.pushRemote, remote.pushDefault, branch.<b>.remote, origin) and
    the same branch name, which is what `git push <remote> <branch>` updates.
    Never `@{u}`: an inherited upstream is not where the branch was pushed.
    """
    ref = git(top, "rev-parse", "--symbolic-full-name", "@{push}", ok_fail=True)
    remotes = sorted((git(top, "remote") or "").split(), key=len, reverse=True)
    if ref and ref.startswith("refs/remotes/"):
        rest = ref[len("refs/remotes/"):]
        for r in remotes:
            if rest.startswith(r + "/"):
                return r, rest[len(r) + 1:], ref
    remote = next((v for v in (
        git(top, "config", "--get", f"branch.{branch}.pushRemote", ok_fail=True),
        git(top, "config", "--get", "remote.pushDefault", ok_fail=True),
        git(top, "config", "--get", f"branch.{branch}.remote", ok_fail=True),
    ) if v and v != "."), "origin")
    return remote, branch, f"refs/remotes/{remote}/{branch}"


def pr_list(gh: str, nwo: str, branch: str) -> list:
    return gh_json(gh, "pr", "list", "-R", nwo, "--head", branch, "--state", "all",
                   "--limit", "5", "--json", "number,url,state,statusCheckRollup")


def inspect(top: str, created: bool, gh: str | None, yq: str | None) -> list[tuple[str, str]]:
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

    remote, dest, ref = push_target(top, branch)
    push_cmd = f"git -C {q} push {shlex.quote(remote)} " + (
        shlex.quote(branch) if dest == branch else f"HEAD:{shlex.quote(dest)}")
    prs = None
    if git(top, "rev-parse", "--verify", "--quiet", ref, ok_fail=True):
        ahead = int(git(top, "rev-list", "--count", f"{ref}..HEAD") or 0)
        if ahead:
            gaps.append((f"{ahead} commit(s) on {branch} not pushed", push_cmd))
            return gaps
    elif branch != default:
        # No remote ref. A PR for this head proves it was pushed (and the
        # remote branch deleted after merge); otherwise it never was.
        if nwo and gh:
            prs = pr_list(gh, nwo, branch)
        if not prs:
            base = git(top, "rev-parse", "--verify", "--quiet", f"origin/{default}", ok_fail=True)
            ahead = int(git(top, "rev-list", "--count", f"origin/{default}..HEAD") or 0) if base else 1
            if ahead:
                gaps.append((f"branch {branch} was never pushed",
                             f"git -C {q} push -u {shlex.quote(remote)} {shlex.quote(branch)}"))
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
    if prs is None:
        prs = pr_list(gh, nwo, branch)
    if not prs:
        has_commits = int(git(top, "rev-list", "--count", f"origin/{default}..HEAD") or 0)
        if has_commits and not pr_gated(top, gh, nwo, yq):
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
    elif pr.get("state") == "MERGED" and primary and not dirty:
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


def decide(payload: dict, home: str, gh: str | None, yq: str | None) -> str | None:
    """The informer text for this prompt, or None when there is nothing to say."""
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
            return top, inspect(top, top in created, gh, yq), None
        except Unverified as exc:
            return top, [], str(exc)

    with ThreadPoolExecutor(max_workers=len(tops)) as pool:
        results = list(pool.map(one, tops))

    gaps = [(top, g, c) for top, found, _ in results for g, c in found]
    unverified = [f"{top} ({err})" for top, _, err in results if err]
    notice = ("Completion check: could not measure " + "; ".join(unverified)) if unverified else None
    if not gaps:
        return notice

    lines = [f"{i}. {top}: {g}\n   next: {c}" for i, (top, g, c) in enumerate(gaps, 1)]
    facts = ("Completion check: work this session touched is not finished:\n" + "\n".join(lines))
    return facts + (
        "\nDone means pushed, PR open with green checks (the merge click is the user's), "
        "and after a merge the primary checkout back on the default branch at latest. "
        "This is status, not an instruction: close what belongs to the current task, and "
        "leave the rest."
    ) + (("\n" + notice) if notice else "")


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        text = decide(payload, str(pathlib.Path.home()), shutil.which("gh"), shutil.which("yq"))
    except Exception:  # a checker that crashes must not wedge the turn
        return 0
    if text:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit", "additionalContext": text}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
