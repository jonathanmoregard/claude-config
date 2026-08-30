#!/usr/bin/env python3
"""dod-check — is this branch's work actually done?

The deterministic half of the definition-of-done system. Answers, from
observable repo and GitHub state alone, whether a branch has reached:

    committed -> pushed -> PR open -> required checks green -> mergeable

...and whether the diff got there honestly (see INTEGRITY below).

It reads evidence, never explanations. Nothing here consults a model, reads
the transcript, or trusts a claim. That is the whole point: the skill and the
Stop gate both grade against this script's exit code, so "CI is green" stops
being something an agent can assert and becomes something it has to earn.

USAGE
    dod-check.py [--repo PATH] [--json] [--quiet] [--no-integrity]

EXIT CODES
    0  done         every gate passed
    1  not done     blockers present (see .blockers)
    2  n/a          not a git repo, or no remote / no GitHub remote
    3  error        could not determine state (treated as NOT done by callers)

Exit 3 matters: an unreachable API, a missing `gh`, a missing `git`, or a usage
error must never read as success. Callers treat 3 like 1. Exit 2 is the one code
that means "stop looking", so nothing that FAILED may borrow it.

JSON NOTE: `repo.dirty` is TRI-STATE — true (uncommitted work), false (clean),
or null (`git status` could not be read, which raises TREE_UNKNOWN). A caller
writing `if repo["dirty"]` gets "clean" for null; test the null explicitly.
`repo.dirty_error` carries why, when there is a why.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

TIMEOUT = 45

# ---------------------------------------------------------------- primitives


def run(cmd, cwd=None, timeout=TIMEOUT):
    """Run a command; return (exit_code, stdout, stderr). Never raises."""
    try:
        p = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s: {' '.join(cmd)}"
    except (OSError, ValueError) as exc:
        return 127, "", f"{type(exc).__name__}: {exc}"


def git(args, cwd, timeout=TIMEOUT):
    return run(["git", *args], cwd=cwd, timeout=timeout)


class Blockers:
    """Ordered, de-duplicated blocker accumulator."""

    def __init__(self):
        self._items = []
        self._seen = set()

    def add(self, code, detail, fix=""):
        if code in self._seen:
            return
        self._seen.add(code)
        self._items.append({"code": code, "detail": detail, "fix": fix})

    def __len__(self):
        return len(self._items)

    def as_list(self):
        return list(self._items)


# ------------------------------------------------------------------ git state


# `collect_repo` answers "the DoD does not apply here" and "I could not find out"
# through one string, and the two must not be confused: n/a is exit 2, which the
# Stop gate reads as nothing to check. A reason carrying this prefix is the
# second kind and becomes exit 3.
GIT_UNAVAILABLE = "git could not run"


def _git_unavailable(code):
    """True for run()'s OWN codes: 124 timed out, 127 could not be spawned.

    These never come from git. EVERY early call has to test them, not just the
    first — measured 2026-08-30, a stub git that answered `rev-parse
    --show-toplevel` and then failed on `git remote` still produced n/a, and the
    gate skipped the repo, unlinked the armfile and ended the turn with a real
    unpushed commit sitting in it. Nine of nine later-call cases were exposed;
    the guard covered only the first.
    """
    return code in (124, 127)


def _unavailable(err, what):
    return f"{GIT_UNAVAILABLE} ({what}): {(err or 'no detail').splitlines()[0][:100]}"


def default_branch(cwd):
    """(name_or_None, git_could_not_run). None means undeterminable."""
    code, out, _ = git(
        ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd, timeout=10
    )
    if _git_unavailable(code):
        return None, True
    if code == 0 and out:
        return out.split("/", 1)[-1], False
    for candidate in ("main", "master"):
        code, _, _ = git(
            ["show-ref", "--verify", "--quiet", f"refs/remotes/origin/{candidate}"],
            cwd,
            timeout=10,
        )
        if _git_unavailable(code):
            return None, True
        if code == 0:
            return candidate, False
    return None, False


def collect_repo(cwd):
    """(facts, reason, fatal).

    `facts` is None when there is nothing to measure; `fatal` says that was
    because git could not RUN, which is exit 3 rather than n/a. That distinction
    used to ride on a PREFIX in the human-readable reason — reword the sentence
    and the exit code silently flips to the fail-open one. It is a separate
    return value now, so no wording can lose it.
    """
    code, root, err = git(["rev-parse", "--show-toplevel"], cwd, timeout=10)
    if _git_unavailable(code):
        return None, _unavailable(err, "rev-parse --show-toplevel"), True
    if code != 0:
        # git genuinely exits 128 outside a repository, so this one really is n/a.
        return None, "not a git repository", False

    code, branch, err = git(["rev-parse", "--abbrev-ref", "HEAD"], root, timeout=10)
    if _git_unavailable(code):
        return None, _unavailable(err, "rev-parse --abbrev-ref HEAD"), True
    if code != 0 or not branch or branch == "HEAD":
        return None, "detached HEAD or unreadable branch", False

    code, remotes, err = git(["remote"], root, timeout=10)
    if _git_unavailable(code):
        return None, _unavailable(err, "git remote"), True
    if code != 0 or not remotes.strip():
        return None, "no git remote configured", False

    base, git_gone = default_branch(root)
    if git_gone:
        return None, _unavailable(None, "default-branch lookup"), True
    if base is None:
        return None, "could not determine the default branch", False

    # Being ON the default branch used to be n/a, which meant the whole gate
    # could be side-stepped by never opening a PR: three commits sitting on an
    # unpushed main read as "done" because nothing was measured. Where pushing
    # straight to the default branch is how the work ships, that IS the ladder —
    # committed, pushed, and the local branch level with origin — so it gets
    # measured under a shorter ladder rather than waved through.
    on_default = branch == base

    # Read the EXIT CODE, not just stdout. `run()` returns ('', err) with 124 on
    # timeout and 127 when git cannot be spawned at all, and git itself exits 128
    # on an unreadable index — every one of which used to render as an empty
    # porcelain listing, i.e. a clean tree. Reproduced with the index at mode
    # 000: `git status --porcelain` exits 128 with nothing on stdout while
    # rev-parse and rev-list still answer, so a tree full of uncommitted work
    # reported DONE. None means "not established", which is never "clean".
    code, status_out, status_err = git(["status", "--porcelain"], root)
    dirty = bool(status_out) if code == 0 else None
    dirty_error = None if code == 0 else (
        (status_err or f"git status exited {code}").splitlines()[0][:120])

    # Refresh the remote-tracking refs first. "Behind origin" and "commits the
    # merge left behind" are both measured against origin/<base>, and a stale
    # ref answers both with a confident zero — the failure mode this tool exists
    # to remove. Only remote-tracking refs are touched: no working tree, no
    # local branch, no index, so this stays a read as far as the repo is
    # concerned. Best-effort: offline or slow is a warning, never a verdict.
    # Retried ONCE, because a failed fetch now BLOCKS (REMOTE_UNVERIFIED on both
    # ladders) and a transient loss must not read as a permanent one. The
    # realistic transient here is CONTENTION rather than the network: ralph's
    # inline handover call and the standalone Stop gate both spawn dod-check on
    # the same repository within one turn, and the loser finds FETCH_HEAD.lock
    # held by the winner. One retry costs nothing on the happy path and removes
    # a false block on a perfectly healthy repo — and a gate that blocks healthy
    # work is a gate somebody switches off.
    fetch_warning = None
    for attempt in (1, 2):
        code, _, err = git(["fetch", "--quiet", "origin", base], root, timeout=25)
        if code == 0:
            fetch_warning = None
            break
        fetch_warning = (
            "could not fetch origin/%s (%s) — 'behind' and 'stranded commit' "
            "counts may be stale" % (base, (err or "no detail").splitlines()[0][:80])
        )
        if attempt == 1:
            time.sleep(1.0)

    # On the default branch, measure against origin/<base> rather than @{u}.
    # An upstream ref is a local convenience that a single `git push` without
    # `-u` never sets, and a branch level with origin then reported as
    # "never pushed" — wrong, and unfixable by the agent, which would push and
    # see the same blocker again. origin/<base> is the thing that actually
    # decides whether the work has left the machine.
    #
    # `behind` is the other half: "main is checked out at latest" is part of
    # done, so commits on origin that are not here are a blocker too.
    ahead_of_default = behind = None
    code, count, _ = git(["rev-list", "--count", f"origin/{base}..HEAD"], root, timeout=15)
    if code == 0 and count.isdigit():
        ahead_of_default = int(count)
    code, count, _ = git(["rev-list", "--count", f"HEAD..origin/{base}"], root, timeout=15)
    if code == 0 and count.isdigit():
        behind = int(count)

    # Unpushed work. No upstream at all counts as unpushed, not as an error:
    # a branch that has never been pushed is exactly the state we want to flag.
    code, upstream, _ = git(
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], root, timeout=10
    )
    if code != 0 or not upstream:
        unpushed, has_upstream = None, False
    else:
        has_upstream = True
        code, count, _ = git(["rev-list", "--count", "@{u}..HEAD"], root, timeout=15)
        unpushed = int(count) if code == 0 and count.isdigit() else None

    return {
        "root": root,
        "branch": branch,
        "default_branch": base,
        "on_default": on_default,
        "dirty": dirty,
        "dirty_error": dirty_error,
        "has_upstream": has_upstream,
        "unpushed": unpushed,
        "ahead_of_default": ahead_of_default,
        "behind": behind,
        "fetch_warning": fetch_warning,
    }, None, False


# ----------------------------------------------------------------- integrity

# Patterns that make a red suite green without making the code right.
# Catalogue adapted from proctor / veredicto (2026). Each entry:
#   (code, human description, compiled regex applied to ADDED diff lines)
#
# Deliberately narrow. A false positive here costs the user real trust, so
# these match syntax that is unambiguous about intent, not merely suspicious.
# Each entry additionally carries a path scope, because an unscoped scan flags
# its own source: this file contains the literal `--passWithNoTests` as a regex,
# and the test harness writes `continue-on-error: true` into a fixture. A
# detector that fires on every repo containing a linter is a detector nobody
# leaves switched on.
ADDED_PATTERNS = [
    (
        "CI_CONTINUE_ON_ERROR",
        "workflow step set to continue-on-error",
        re.compile(r"^\s*continue-on-error\s*:\s*true", re.I),
        re.compile(r"\.github/workflows/.*\.ya?ml$|\.gitlab-ci\.ya?ml$", re.I),
    ),
    (
        "TEST_PASS_WITH_NO_TESTS",
        "test runner told to pass when no tests match",
        re.compile(r"--passWithNoTests\b"),
        re.compile(r"(package\.json|Makefile|\.ya?ml|\.toml|\.sh)$", re.I),
    ),
    (
        "TEST_SWALLOWED",
        "test command result discarded with `|| true`",
        re.compile(r"\b(pytest|jest|vitest|cargo\s+test|go\s+test|npm\s+test)\b.*\|\|\s*true\s*$"),
        re.compile(r"(Makefile|\.ya?ml|\.sh|justfile)$", re.I),
    ),
    (
        "TEST_SKIPPED",
        "test skipped or ignored",
        re.compile(
            r"(^\s*@pytest\.mark\.(skip|xfail)\b|"
            r"^\s*@(unittest\.)?skip\b|"
            r"^\s*#\[ignore\]|"
            r"\b(it|test|describe|context)\.skip\s*\(|"
            r"^\s*\bx(it|describe)\s*\(|"
            r"\bt\.Skip\s*\()"
        ),
        None,  # test files only — enforced below via TEST_PATH
    ),
    (
        "ASSERT_TAUTOLOGY",
        "assertion that cannot fail",
        re.compile(
            r"^\s*(assert\s+True\s*$|assert\s*\(\s*true\s*\)|"
            r"expect\s*\(\s*true\s*\)\s*\.\s*to\w*\s*(be|equal)\s*\(\s*true\s*\)|"
            r"assertTrue\s*\(\s*True\s*\))",
            re.I,
        ),
        None,
    ),
]

# Files whose deletion is a finding in its own right.
TEST_PATH = re.compile(
    r"(^|/)(tests?|spec|__tests__|e2e)/|"
    r"(^|/)test_[^/]+\.py$|"
    r"[._-](test|spec)\.[a-z]+$|"
    r"_test\.(go|py|rs)$",
    re.I,
)

# proctor's escape hatch: a line carrying a justification comment is exempt.
# Writing the sentence puts it in the diff where a human can disagree with it.
JUSTIFIED = re.compile(r"(#|//|<!--)\s*dod-ok\b")


def ci_configured(root, base):
    """Does this repo actually have CI that a PR should trigger?

    Zero checks means two completely different things, and conflating them is
    what made this predicate unusable: a repo that DELETED its workflows is
    green-by-vandalism, while a repo that never had any has simply nothing to
    wait for. Blocking the second one asks the agent to sit forever on a check
    that is never coming.

    So this measures the branch, not the verdict: workflow files on the head OR
    on the base. Base matters as much as head — a branch that removes the only
    workflow keeps CI "expected", which is exactly the case the NO_CHECKS
    blocker was written to catch.

    Returns (True | False | None, note). None means undeterminable, and the
    caller treats that as expected — the safe direction, per COULD NOT MEASURE.
    """
    found = []
    unreadable = []
    for ref in ("HEAD", f"origin/{base}"):
        code, names, _ = git(
            ["ls-tree", "-r", "--name-only", ref, "--", ".github/workflows"],
            root,
            timeout=15,
        )
        if code != 0:
            unreadable.append(ref)
            continue
        for path in names.splitlines():
            if path.strip().endswith((".yml", ".yaml")):
                found.append((ref, path.strip()))

    if not found:
        # ANY unreadable ref means "could not confirm", not "no CI". Requiring
        # BOTH to fail meant the ordinary case — HEAD readable with no
        # workflows, origin/<base> missing — returned False, and False here is
        # the answer that waves the branch through. Reproduced 2026-08-30 by
        # renaming the base branch upstream while the local origin/HEAD symref
        # still named the old one: `git fetch origin <base>` then fails, the
        # remote-tracking ref stays absent, and a branch that had DELETED its
        # only workflow reported DONE with no NO_CHECKS blocker. No tampering
        # required — an upstream rename does it.
        if unreadable:
            return None, ("could not read %s for workflow files"
                          % ", ".join(unreadable))
        return False, ""

    # A workflow file that only runs on a schedule or a manual dispatch produces
    # no PR checks and must not be held against the branch. Substring scan, not
    # a YAML parse: any mention of a PR- or push-triggered event counts, so the
    # error lands on the side of keeping the blocker.
    for ref, path in found:
        code, body, _ = git(["show", f"{ref}:{path}"], root, timeout=15)
        if code != 0:
            return None, f"could not read {path}"
        if "pull_request" in body or "push" in body:
            return True, f"{path} ({'head' if ref == 'HEAD' else 'base'})"

    return False, "workflows exist but none trigger on pull_request or push"


def collect_integrity(root, base):
    """Scan the branch diff for green-by-weakening patterns."""
    code, mb, _ = git(["merge-base", "HEAD", f"origin/{base}"], root, timeout=20)
    if code != 0 or not mb:
        return None, "could not compute merge-base; integrity scan skipped"

    findings = []

    # Deleted or renamed-away test files.
    code, names, _ = git(
        ["diff", "--diff-filter=D", "--name-only", f"{mb}...HEAD"], root, timeout=30
    )
    if code == 0:
        for path in (p for p in names.splitlines() if p.strip()):
            if TEST_PATH.search(path):
                findings.append(
                    {
                        "code": "TEST_FILE_DELETED",
                        "detail": f"test file deleted: {path}",
                        "path": path,
                    }
                )

    # Added lines matching a weakening pattern.
    code, diff, _ = git(["diff", "--unified=0", f"{mb}...HEAD"], root, timeout=45)
    if code != 0:
        return findings, "could not read the branch diff; pattern scan incomplete"

    current = None
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        added = line[1:]
        if JUSTIFIED.search(added):
            continue
        for code_name, desc, rx, scope in ADDED_PATTERNS:
            # `scope is None` means "test files only": a skipped test or a
            # tautological assertion is only meaningful where tests live.
            if scope is None:
                if not (current and TEST_PATH.search(current)):
                    continue
            elif not (current and scope.search(current)):
                continue
            if rx.search(added):
                findings.append(
                    {
                        "code": code_name,
                        "detail": f"{desc} — {current or '?'}: {added.strip()[:120]}",
                        "path": current,
                    }
                )
                break

    return findings, None


# -------------------------------------------------------------------- github


def open_prs_for_branch(root, branch):
    """Every open PR whose head is this branch. (list_or_None, error_or_None).

    `gh pr view` with no argument resolves exactly ONE pull request for the
    current branch and says nothing about having chosen. Two open PRs off one
    head — the ordinary "same work, one into main and one into release" shape —
    therefore produced a verdict about whichever came back, so a green PR into
    release could report DONE while the one into main sat BLOCKED. Asking for
    the list is the only way to notice the ambiguity exists.
    """
    code, out, err = run(
        ["gh", "pr", "list", "--head", branch, "--state", "open",
         "--json", "number,url,baseRefName"],
        cwd=root,
    )
    if code != 0:
        return None, (err or "gh pr list failed").splitlines()[0][:160]
    try:
        prs = json.loads(out or "[]")
    except ValueError:
        return None, "gh pr list returned unparseable JSON"
    return (prs if isinstance(prs, list) else None), None


def collect_pr(root):
    """PR facts via gh. Returns (pr_or_None, checks_or_None, error_or_None)."""
    if shutil.which("gh") is None:
        return None, None, "gh CLI not found on PATH"

    # Every name here must exist in `gh pr view --json`'s field list. It is NOT
    # the GraphQL schema: `reviewThreads` is a valid GraphQL connection and an
    # invalid gh field, and asking for it makes gh reject the whole call. A
    # contract test pins this list against the installed gh.
    fields = (
        "number,url,state,isDraft,mergeStateStatus,mergeable,"
        "reviewDecision,headRefName,baseRefName,title"
    )
    code, out, err = run(["gh", "pr", "view", "--json", fields], cwd=root)
    if code != 0:
        low = (err or "").lower()
        if "no pull requests found" in low or "no default remote" in low:
            return None, None, None  # genuinely no PR yet — a blocker, not an error
        return None, None, f"gh pr view failed: {err[:200]}"

    try:
        pr = json.loads(out)
    except ValueError:
        return None, None, "gh pr view returned unparseable JSON"
    # Shape-checked like open_prs_for_branch already was. A list or a string here
    # reaches `pr.get(...)` and raises past every handler into main()'s blanket
    # except, which reports exit 3 with an empty blockers list — a whole-predicate
    # failure presented as a measurement that found nothing.
    if not isinstance(pr, dict):
        return None, None, f"gh pr view returned {type(pr).__name__}, expected an object"

    checks, cerr = collect_checks(root)
    return pr, checks, cerr


def collect_checks(root):
    """Check rollup bucketed pass/fail/pending/skipping.

    `gh pr checks` exits 0 (all pass), 1 (some fail), 8 (pending), and errors
    when the PR has no checks at all. All four are meaningful, so the exit code
    is never treated as failure on its own.
    """
    code, out, err = run(
        ["gh", "pr", "checks", "--json", "name,state,bucket,link,workflow"], cwd=root
    )

    if not out:
        low = (err or "").lower()
        if "no checks reported" in low or "no checks" in low:
            return {
                "total": 0,
                "pass": 0,
                "fail": 0,
                "pending": 0,
                "skipping": 0,
                "failing": [],
                "unrecognised": [],
            }, None
        return None, f"gh pr checks failed: {err[:200]}"

    try:
        rows = json.loads(out)
    except ValueError:
        return None, "gh pr checks returned unparseable JSON"
    if not isinstance(rows, list):
        return None, f"gh pr checks returned {type(rows).__name__}, expected a list"
    rows = [r for r in rows if isinstance(r, dict)]

    tally = {"pass": 0, "fail": 0, "pending": 0, "skipping": 0, "cancel": 0}
    failing = []
    # A bucket outside the five gh documents counted toward `total` and toward
    # nothing else: not fail, not pending, so the caller's all-clear branch fired
    # over a check whose state was never read. gh has added bucket values before,
    # and an empty `bucket` is what a partially-populated row gives. Unknown is
    # tracked by name so it can be reported rather than absorbed.
    unrecognised = []
    for row in rows:
        bucket = (row.get("bucket") or "").lower()
        tally[bucket] = tally.get(bucket, 0) + 1
        if bucket not in ("pass", "fail", "pending", "skipping", "cancel"):
            unrecognised.append(
                f"{row.get('name') or row.get('workflow') or '?'}"
                f"={bucket or '(empty)'}")
        if bucket in ("fail", "cancel"):
            failing.append(
                {
                    "name": row.get("name") or row.get("workflow") or "?",
                    "bucket": bucket,
                    "link": row.get("link") or "",
                }
            )

    return {
        "total": len(rows),
        "pass": tally.get("pass", 0),
        "fail": tally.get("fail", 0) + tally.get("cancel", 0),
        "pending": tally.get("pending", 0),
        "skipping": tally.get("skipping", 0),
        "failing": failing,
        "unrecognised": unrecognised,
    }, None


GRAPHQL_THREADS = """
query($owner:String!, $repo:String!, $num:Int!) {
  repository(owner:$owner, name:$repo) {
    pullRequest(number:$num) {
      reviewThreads(first:100) {
        nodes { isResolved isOutdated }
        pageInfo { hasNextPage }
      }
    }
  }
}
"""


def unresolved_threads(root, number):
    """Count unresolved review threads.

    Only reachable over GraphQL — `gh pr view --json reviewThreads` is not a
    thing, which is the bug this function was rewritten to fix. Soft-fails: a
    missing thread count is a warning, never a silent zero, because "0
    unresolved" and "could not count" must not look the same to the caller.
    """
    code, out, err = run(
        ["gh", "repo", "view", "--json", "owner,name"], cwd=root, timeout=20
    )
    if code != 0:
        return None, f"could not resolve owner/repo: {err[:120]}"
    try:
        meta = json.loads(out)
        owner = meta["owner"]["login"]
        name = meta["name"]
    except (ValueError, KeyError, TypeError):
        return None, "could not parse owner/repo"

    code, out, err = run(
        ["gh", "api", "graphql", "-f", f"query={GRAPHQL_THREADS}",
         "-F", f"owner={owner}", "-F", f"repo={name}", "-F", f"num={number}"],
        cwd=root, timeout=30,
    )
    if code != 0:
        return None, f"review-thread query failed: {err[:120]}"
    # The sum() belongs INSIDE the try. `nodes: null` and non-dict entries both
    # pass the lookups above and then raise on iteration, and nothing here
    # caught that: a designed soft-fail warning escalated into a whole-predicate
    # crash caught only by main()'s blanket handler, which reports exit 3 with
    # an empty blockers list.
    try:
        conn = (json.loads(out)["data"]["repository"]["pullRequest"]
                ["reviewThreads"])
        # An outdated thread hangs off a superseded diff; it does not gate a
        # merge.
        unresolved = sum(1 for t in conn["nodes"]
                         if not t.get("isResolved") and not t.get("isOutdated"))
    except (ValueError, KeyError, TypeError, AttributeError):
        return None, "review-thread query returned an unexpected shape"

    # `first:100` is a PAGE, not the set. Threads past the hundredth simply did
    # not exist as far as this function was concerned, so a truncated page of
    # resolved threads printed as a clean sheet — the same "could not look"
    # rendered as "looked and found nothing" that the error path above refuses.
    # A non-zero count still blocks (the number is then a floor, and the blocker
    # says the same thing either way); only a zero has to be disowned.
    try:
        truncated = bool((conn.get("pageInfo") or {}).get("hasNextPage"))
    except AttributeError:
        truncated = False
    if truncated and not unresolved:
        return None, ("this PR has more than 100 review threads and only the "
                      "first 100 were read; unresolved threads were NOT counted")
    return unresolved, None


# ------------------------------------------------------------------ evaluate

# mergeStateStatus values that mean "a human cannot press merge right now".
# CLEAN and HAS_HOOKS are fine; UNSTABLE means non-required checks are red,
# which the required-checks tally already judges, so it is not a blocker here.
NOT_MERGEABLE = {"DIRTY": "merge conflicts with the base branch",
                 "BLOCKED": "blocked by branch protection (missing review or required check)",
                 "BEHIND": "branch is behind the base and must be updated",
                 "DRAFT": "pull request is still a draft"}


def evaluate(cwd, do_integrity=True):
    result = {
        "done": False,
        "applicable": True,
        "merged": False,
        "blockers": [],
        "repo": None,
        "pr": None,
        "checks": None,
        "integrity": [],
        "warnings": [],
        "error": None,
    }

    repo, na_reason, fatal = collect_repo(cwd)
    if repo is None:
        # "The DoD does not apply here" and "I could not find out" are different
        # answers and get different exit codes. Only the first is n/a, and which
        # one this is arrives as its own value rather than as a prefix somebody
        # could reword away.
        if fatal:
            result["error"] = na_reason
            return result, 3
        result.update(applicable=False, done=False, error=None)
        result["reason"] = na_reason
        return result, 2
    result["repo"] = repo
    if repo.get("fetch_warning"):
        result["warnings"].append(repo["fetch_warning"])

    b = Blockers()

    if repo["dirty"] is None:
        b.add("TREE_UNKNOWN",
              "could not read the working tree state "
              f"({repo.get('dirty_error') or 'git status failed'}), so "
              "uncommitted work has NOT been ruled out",
              "fix whatever stopped `git status` from running and re-check — "
              "an unreadable tree is not a clean one")
    elif repo["dirty"]:
        b.add("DIRTY_TREE", "uncommitted changes in the working tree",
              "commit or stash them")

    # Both ladders treat an UNCOUNTABLE commit gap the same way, which they did
    # not: `None` from a failed rev-list is falsy, so the feature-branch arm
    # below fell through to no blocker at all while the default-branch arm above
    # raised one on the identical value. Same failure, opposite verdicts,
    # decided by which branch the repo happened to be on.
    if repo["on_default"]:
        # Measured against origin/<base>, not @{u} — see collect_repo.
        if repo["ahead_of_default"]:
            b.add("UNPUSHED_COMMITS",
                  f"{repo['ahead_of_default']} commit(s) on "
                  f"{repo['branch']} not pushed to origin",
                  f"git push origin {repo['branch']}")
        elif repo["ahead_of_default"] is None:
            b.add("UNPUSHED_UNKNOWN",
                  f"could not count commits against origin/"
                  f"{repo['default_branch']} — whether this branch is pushed "
                  "was NOT established",
                  f"check the remote-tracking refs, then re-run; "
                  f"`git push -u origin {repo['branch']}` if it never landed")
    elif not repo["has_upstream"]:
        b.add("NOT_PUSHED", "branch has no upstream — never pushed",
              f"git push -u origin {repo['branch']}")
    elif repo["unpushed"] is None:
        b.add("UNPUSHED_UNKNOWN",
              "could not count commits against the upstream ref, so unpushed "
              "work has NOT been ruled out",
              "check the remote-tracking refs (`git fetch origin`), then "
              "re-run — an uncountable gap is not an empty one")
    elif repo["unpushed"]:
        b.add("UNPUSHED_COMMITS", f"{repo['unpushed']} commit(s) not pushed",
              "git push")

    # A failed fetch is not a warning, it is a MISSING MEASUREMENT, and it is
    # missing on BOTH ladders — which is where this used to sit wrong. It was
    # scoped to the default branch on the reasoning that a feature branch's gh
    # calls do their own measuring and fail loudly. They do, but they measure
    # the PULL REQUEST; origin/<base> is what answers "behind", "commits the
    # merge left behind", "does this repo have CI", and the integrity scan's
    # merge-base. Measured 2026-08-30: with origin/<base> unreadable, a branch
    # that had DELETED its only workflow reported DONE — ci_configured saw one
    # unreadable ref, concluded "no CI in this repo", and the integrity scan
    # skipped itself in the same breath. Three silent degradations, one cause,
    # and on the feature-branch ladder none of them blocked.
    #
    # This does not newly hang offline sessions: offline, the gh calls below
    # fail and exit 3 already blocks.
    if repo.get("fetch_warning"):
        b.add("REMOTE_UNVERIFIED",
              f"could not reach origin/{repo['default_branch']}, so nothing "
              "measured against it was established — 'behind', stranded "
              "commits, whether this repo has CI, and the integrity scan all "
              "read from that ref",
              "fix connectivity and re-run — do not read the previous fetch's "
              "refs as current")

    # The integrity scan runs for BOTH ladders, which is why it sits above the
    # direct-push return rather than at the end of the function. It used to be
    # below it, so work pushed straight to the default branch was never scanned
    # for deleted tests, `continue-on-error`, `|| true` or a skipped test —
    # reproduced 2026-08-30 with all three weakenings: DONE on the direct-push
    # ladder, INTEGRITY on the PR ladder, same commit.
    #
    # ONE HONEST LIMIT, established by the same repro. collect_integrity diffs
    # merge-base(HEAD, origin/<base>)...HEAD, and reaching DONE on the
    # direct-push ladder REQUIRES being level with origin — at which point the
    # merge-base IS head and the range is empty by construction. So this catches
    # a weakening in the window where it can still be repaired (committed, not
    # yet pushed — a state the Stop gate measures every turn), not after it has
    # shipped. Moving the call is necessary and not sufficient; the residue is
    # recorded here rather than papered over.
    if do_integrity:
        findings, warn = collect_integrity(repo["root"], repo["default_branch"])
        if warn:
            result["warnings"].append(warn)
        if findings:
            result["integrity"] = findings
            codes = ", ".join(sorted({f["code"] for f in findings}))
            b.add("INTEGRITY", f"diff weakens the test/CI signal: {codes}",
                  "revert the weakening, or justify the line with a `dod-ok` comment")

    # ---- direct-push ladder -------------------------------------------------
    # On the default branch there is no PR to open and no review to wait for.
    # Done is: committed, pushed, and level with origin. Stopping here also
    # keeps the check honest — demanding a PR that cannot exist would make the
    # gate unsatisfiable on exactly the repos where pushing to main is normal.
    if repo["on_default"]:
        # REMOTE_UNVERIFIED is raised above, for both ladders. It was measured
        # here first: with a collaborator's commit on origin and the remote made
        # unreachable, this ladder reported DONE / exit 0 while genuinely
        # behind — "green" and "green as far as I got" printing identically.
        if repo["behind"] is None:
            # The uncountable case, which `if repo["behind"]:` swallowed as zero.
            # A failed FETCH is covered by REMOTE_UNVERIFIED above; this is the
            # narrower one the reviewer isolated — the fetch worked and the
            # rev-list did not, so "level with origin" was never established
            # while the ladder's terminal condition IS being level with origin.
            b.add("BEHIND_UNKNOWN",
                  f"could not count commits on origin/{repo['default_branch']} "
                  "that are missing here, so 'up to date' was NOT established",
                  f"git fetch origin {repo['default_branch']} && re-run")
        elif repo["behind"]:
            b.add("BEHIND_REMOTE",
                  f"{repo['behind']} commit(s) on origin/{repo['default_branch']} "
                  "not in the working copy",
                  f"git pull --ff-only origin {repo['default_branch']}")
        result["blockers"] = b.as_list()
        result["done"] = len(b) == 0
        return result, (0 if result["done"] else 1)

    pr, checks, gh_error = collect_pr(repo["root"])
    if gh_error:
        # A remote that simply is not GitHub is NOT a measurement failure — there
        # is no PR to find and never will be. Treating it as exit 3 made the gate
        # unsatisfiable: it blocked every turn until the budget burned.
        if "known github host" in gh_error.lower():
            # ...but only the PULL-REQUEST HALF of the ladder is inapplicable.
            # The committed -> pushed half was measured above, and this return
            # used to drop it. Reproduced 2026-08-30 on a GitLab remote with a
            # dirty tree AND an unpushed commit: exit 2, `blockers: []`, and
            # "n/a — nothing to open a PR against", while the same payload's
            # repo block said dirty: true, ahead_of_default: 1. The exit-3
            # branch below kept its accumulator all along — this was the
            # asymmetry, and exit 2 is the worse of the two to lose blockers
            # on, because the Stop hook reads 2 as "nothing to check here" and
            # ends the turn.
            result["blockers"] = b.as_list()
            result["reason"] = ("no GitHub remote — the pull-request half of "
                                "the ladder does not apply here")
            if len(b):
                result["done"] = False
                return result, 1
            result.update(applicable=False, error=None)
            return result, 2
        result["error"] = gh_error
        result["blockers"] = b.as_list()
        return result, 3

    result["pr"] = pr
    result["checks"] = checks

    if pr is None:
        b.add("NO_PR", "no pull request open for this branch",
              "gh pr create --fill")
    elif (pr.get("state") or "").upper() == "MERGED":
        # Already landed. Every downstream gate is moot, and asking the user to
        # merge it is the specific failure this branch of the logic exists to
        # prevent: a stale PR link presented as a live request.
        result["merged"] = True
        # Landed, but the repo is still parked on the merged branch. Putting it
        # back is the last step of the work, not a chore for Jonathan — leaving
        # a checkout on a dead branch is exactly the "closing ceremony" this is
        # meant to remove. The gate SAYS so and the agent does it, visibly; the
        # predicate never touches the repo itself. That is deliberate: an
        # automatic checkout from a Stop hook is the failure we watched happen
        # on 2026-08-27, when another agent switched branches under a live
        # session and deleted files out from under a running hook.
        # Commits the merge did not take. A PR merges the commits it had; push
        # more to the branch afterwards and they sit on a dead branch while the
        # PR still reads MERGED — green, and the work is nowhere. Measured
        # against origin/<base>, which collect_repo has just fetched.
        stranded = repo["ahead_of_default"]
        if stranded is None:
            # None is "the rev-list failed", not "nothing was stranded", and
            # collapsing the two emitted NOT_ON_DEFAULT — a confident, WRONG
            # blocker telling the agent to go home while commits the merge did
            # not take may still be sitting on the branch.
            b.add("STRANDED_UNKNOWN",
                  f"PR is merged, but the commits on {repo['branch']} could not "
                  f"be counted against {repo['default_branch']} — whether the "
                  "merge took everything is UNKNOWN",
                  f"git fetch origin {repo['default_branch']} && re-run before "
                  "deleting or leaving this branch")
        elif stranded:
            b.add("COMMITS_AFTER_MERGE",
                  f"{stranded} commit(s) on {repo['branch']} are not in "
                  f"{repo['default_branch']} — the merge did not take them",
                  "open a new PR for them, or cherry-pick them onto "
                  f"{repo['default_branch']}. Do not delete the branch first.")
        else:
            b.add("NOT_ON_DEFAULT",
                  f"PR is merged but the repo is still on {repo['branch']}",
                  f"git switch {repo['default_branch']} && "
                  f"git pull --ff-only origin {repo['default_branch']}")
    elif (pr.get("state") or "").upper() == "CLOSED":
        b.add("PR_CLOSED",
              f"PR #{pr.get('number')} was closed without merging",
              "reopen it, or open a fresh PR — do not report this one as pending")
    else:
        # Ambiguity beats a confident answer about the wrong PR.
        others, list_err = open_prs_for_branch(repo["root"], repo["branch"])
        if list_err:
            result["warnings"].append(f"could not list PRs for this branch: {list_err}")
        elif others is not None and len(others) > 1:
            where = ", ".join(
                f"#{p.get('number')} → {p.get('baseRefName')}" for p in others[:5]
            )
            b.add("MULTIPLE_PRS",
                  f"{len(others)} open PRs share this head branch ({where}); "
                  f"this verdict describes only #{pr.get('number')}",
                  "close the ones that should not exist, or check each "
                  "explicitly with `gh pr view <n>` — a green PR into one base "
                  "says nothing about a blocked one into another")

        if pr.get("isDraft"):
            b.add("PR_DRAFT", "pull request is a draft",
                  f"gh pr ready {pr.get('number')}")

        state_status = (pr.get("mergeStateStatus") or "").upper()
        # UNKNOWN is not CLEAN. GitHub returns it while lazily recomputing, and
        # treating it as "no blocker" produced a DONE verdict on a branch that
        # was two commits behind master. Unknown is measured as unknown, exactly
        # like unreadable check status. (Merged and closed PRs also report
        # UNKNOWN, but both are handled above and never reach here.)
        if state_status in ("", "UNKNOWN"):
            b.add("MERGEABLE_UNKNOWN",
                  "GitHub has not finished computing mergeability "
                  f"(mergeStateStatus={state_status or 'missing'})",
                  "re-run in a few seconds — do not read this as mergeable")

        msg = NOT_MERGEABLE.get(state_status)
        if msg:
            fix = ""
            if (pr.get("mergeStateStatus") or "").upper() == "BEHIND":
                fix = (f"git fetch origin {repo['default_branch']} && "
                       f"git merge origin/{repo['default_branch']} && git push")
            b.add("NOT_MERGEABLE", msg, fix)

        n_unresolved, thread_err = unresolved_threads(repo["root"], pr.get("number"))
        if thread_err:
            # Unknown is not zero. Say so rather than reporting a clean sheet.
            result["warnings"].append(f"unresolved review threads not counted: {thread_err}")
        elif n_unresolved:
            b.add("UNRESOLVED_THREADS",
                  f"{n_unresolved} unresolved review thread(s)",
                  "answer and resolve them")

        if checks is None:
            result["warnings"].append("check status unavailable")
            b.add("CHECKS_UNKNOWN", "could not read check status", "retry")
        elif checks["total"] == 0:
            # "Green because there is no CI" satisfies a naive predicate — but
            # so does "no CI was ever configured", and only the first is a
            # defect. Ask the repo which one this is.
            expected, note = ci_configured(repo["root"], repo["default_branch"])
            if expected is False:
                result["warnings"].append(
                    "no CI configured in this repo — nothing to wait for"
                    + (f" ({note})" if note else "")
                )
            else:
                if expected is None:
                    result["warnings"].append(
                        f"could not confirm CI config: {note} — assuming CI is expected"
                    )
                b.add("NO_CHECKS",
                      "the PR reports no checks, but this repo has CI"
                      + (f" ({note})" if note else ""),
                      "find out why none ran — a deleted, disabled or "
                      "un-triggered workflow is green-by-vandalism")
        else:
            unknown = checks.get("unrecognised") or []
            if unknown:
                # Counted in `total`, tallied as neither fail nor pending — so
                # without this the row fell through to the all-clear branch and
                # a check nobody read contributed to DONE.
                b.add("CHECKS_UNRECOGNISED",
                      f"{len(unknown)} check(s) reported a state this tool does "
                      f"not recognise ({', '.join(unknown[:5])}); they were "
                      "counted as neither passing nor pending",
                      "look at them on the PR — an unread check is not a green "
                      "one. If gh has grown a new bucket, teach dod-check it.")
            # NOTHING actually ran. `total > 0` skips the NO_CHECKS branch above,
            # and the bucket that made the total non-zero is one nobody reads:
            # `skipping` is tallied and then never consulted, so a PR whose every
            # job was skipped — `if:` false, a `paths:` filter the diff misses —
            # reported DONE. Measured 2026-08-30: one skipped job away from the
            # NO_CHECKS branch, and the verdict flips from BLOCKED to green.
            # Deliberately narrow: this fires only when NOTHING passed, failed or
            # is pending. A skipped job alongside real ones is ordinary.
            if (checks["pass"] == 0 and checks["fail"] == 0
                    and checks["pending"] == 0 and checks.get("skipping")):
                b.add("CHECKS_ALL_SKIPPED",
                      f"all {checks['total']} check(s) were SKIPPED — nothing "
                      "actually ran, so nothing was verified",
                      "find out why every job skipped: an `if:` condition or a "
                      "paths filter that excludes this branch is green-by-"
                      "not-running, exactly like a deleted workflow")
            if checks["fail"]:
                names = ", ".join(c["name"] for c in checks["failing"][:5])
                b.add("CHECKS_FAILING",
                      f"{checks['fail']} failing check(s): {names}",
                      "fix them — check the base commit first to rule out a "
                      "pre-existing failure (see hardening-flaky-ci)")
            if checks["pending"]:
                b.add("CHECKS_PENDING",
                      f"{checks['pending']} check(s) still running",
                      "gh pr checks --watch --fail-fast")

    # (the integrity scan ran above, for both ladders — see the comment there)

    result["blockers"] = b.as_list()
    result["done"] = len(b) == 0
    return result, (0 if result["done"] else 1)


# --------------------------------------------------------------------- output


def render(result):
    if not result["applicable"]:
        return f"dod-check: n/a — {result.get('reason', 'unknown')}"

    lines = []
    repo = result.get("repo")
    if repo is None:
        # An error raised BEFORE the repo could be read — `git` itself missing
        # or hung. There is no branch name to head the report with, and reaching
        # for one crashed the renderer, turning a clean exit 3 into a traceback
        # and exit 1. Applicable is true here on purpose: the DoD does apply,
        # we just could not measure it.
        first = (result.get("error") or "unknown").splitlines()[0]
        return ("dod-check: COULD NOT MEASURE\n"
                f"  {first}\n"
                "  NOTHING about this repository was read — not the branch, not "
                "the tree, not the remote. This is not a green result; fix the "
                "tooling first.")
    head = f"{repo['branch']} → {repo['default_branch']}"
    pr = result.get("pr")
    if pr:
        head += f"  (PR #{pr.get('number')})"

    if result["done"]:
        if result.get("merged"):
            lines.append(f"dod-check: MERGED — {head}")
            lines.append("  Already landed. Do NOT ask for a merge; report it as done.")
        else:
            lines.append(f"dod-check: DONE — {head}")
        if pr:
            lines.append(f"  {pr.get('url')}")
        # A DONE verdict must still carry what could not be established.
        # Dropping warnings here would make "green" and "green as far as I got"
        # print identically, which is the failure mode this whole tool exists
        # to remove.
        for warn in result.get("warnings", []):
            lines.append(f"  ! {warn}")
        return "\n".join(lines)

    # An error means the GitHub half never ran. Printing the blockers we DID
    # find underneath it reads as a completed measurement with a short list,
    # which is worse than printing nothing: it invites "only one blocker left".
    # Report the failure and stop.
    if result.get("error"):
        first = result["error"].splitlines()[0]
        lines.append(f"dod-check: COULD NOT MEASURE — {head}")
        lines.append(f"  {first}")
        lines.append("  PR and check state were NOT read. This is not a green "
                     "result — treat it as not done and fix the tooling first.")
        return "\n".join(lines)

    # Merged-but-not-finished still has to say MERGED loudly. The remaining
    # work is tidying (get off the dead branch); the one thing that must not
    # happen is this reading as a live merge request.
    if result.get("merged"):
        lines.append(f"dod-check: MERGED, NOT FINISHED — {head}")
        lines.append("  Already landed. Do NOT ask for a merge — finish the tidy-up:")
    else:
        lines.append(f"dod-check: NOT DONE — {head}")
    for item in result["blockers"]:
        lines.append(f"  [{item['code']}] {item['detail']}")
        if item.get("fix"):
            lines.append(f"      fix: {item['fix']}")
    for finding in result.get("integrity", []):
        lines.append(f"  · {finding['detail']}")
    for warn in result.get("warnings", []):
        lines.append(f"  ! {warn}")
    return "\n".join(lines)


class _Parser(argparse.ArgumentParser):
    """argparse exits 2 on a usage error, and 2 is this tool's n/a code.

    A mistyped flag therefore told every caller "the definition of done does not
    apply to this repository" — the Stop gate reads exit 2 as nothing to check
    here and ends the turn. Usage errors are a failure to measure, so they exit
    3 like every other one. `--help` and `--version` still exit 0; argparse
    raises those through SystemExit directly, not through error().
    """

    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        raise SystemExit(3)


def main():
    ap = _Parser(description="Is this branch's work done?")
    ap.add_argument("--repo", default=os.getcwd(), help="repo path (default: cwd)")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    ap.add_argument("--quiet", action="store_true", help="exit code only")
    ap.add_argument("--no-integrity", action="store_true",
                    help="skip the diff integrity scan")
    args = ap.parse_args()

    try:
        result, code = evaluate(args.repo, do_integrity=not args.no_integrity)
    except Exception as exc:  # never let the gate crash open
        payload = {"done": False, "applicable": True, "blockers": [],
                   "error": f"dod-check crashed: {type(exc).__name__}: {exc}"}
        if args.json:
            print(json.dumps(payload, indent=2))
        elif not args.quiet:
            print(payload["error"], file=sys.stderr)
        return 3

    if args.json:
        print(json.dumps(result, indent=2))
    elif not args.quiet:
        print(render(result))
    return code


if __name__ == "__main__":
    sys.exit(main())
