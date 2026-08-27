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
    2  n/a          not a git repo, on the default branch, or no remote
    3  error        could not determine state (treated as NOT done by callers)

Exit 3 matters: an unreachable API or a missing `gh` must never read as
success. Callers treat 3 like 1.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

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


def default_branch(cwd):
    """origin/HEAD, falling back to main/master. None if undeterminable."""
    code, out, _ = git(
        ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd, timeout=10
    )
    if code == 0 and out:
        return out.split("/", 1)[-1]
    for candidate in ("main", "master"):
        code, _, _ = git(
            ["show-ref", "--verify", "--quiet", f"refs/remotes/origin/{candidate}"],
            cwd,
            timeout=10,
        )
        if code == 0:
            return candidate
    return None


def collect_repo(cwd):
    """Repo facts, or ('n/a', reason) when the DoD does not apply here."""
    code, root, _ = git(["rev-parse", "--show-toplevel"], cwd, timeout=10)
    if code != 0:
        return None, "not a git repository"

    code, branch, _ = git(["rev-parse", "--abbrev-ref", "HEAD"], root, timeout=10)
    if code != 0 or not branch or branch == "HEAD":
        return None, "detached HEAD or unreadable branch"

    code, remotes, _ = git(["remote"], root, timeout=10)
    if code != 0 or not remotes.strip():
        return None, "no git remote configured"

    base = default_branch(root)
    if base is None:
        return None, "could not determine the default branch"

    # Being ON the default branch used to be n/a, which meant the whole gate
    # could be side-stepped by never opening a PR: three commits sitting on an
    # unpushed main read as "done" because nothing was measured. Where pushing
    # straight to the default branch is how the work ships, that IS the ladder —
    # committed, pushed, and the local branch level with origin — so it gets
    # measured under a shorter ladder rather than waved through.
    on_default = branch == base

    dirty = bool(git(["status", "--porcelain"], root)[1])

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
        "has_upstream": has_upstream,
        "unpushed": unpushed,
        "ahead_of_default": ahead_of_default,
        "behind": behind,
    }, None


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
        if unreadable and len(unreadable) == 2:
            return None, "could not read either ref for workflow files"
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
            }, None
        return None, f"gh pr checks failed: {err[:200]}"

    try:
        rows = json.loads(out)
    except ValueError:
        return None, "gh pr checks returned unparseable JSON"

    tally = {"pass": 0, "fail": 0, "pending": 0, "skipping": 0, "cancel": 0}
    failing = []
    for row in rows:
        bucket = (row.get("bucket") or "").lower()
        tally[bucket] = tally.get(bucket, 0) + 1
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
    }, None


GRAPHQL_THREADS = """
query($owner:String!, $repo:String!, $num:Int!) {
  repository(owner:$owner, name:$repo) {
    pullRequest(number:$num) {
      reviewThreads(first:100) { nodes { isResolved isOutdated } }
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
    try:
        nodes = (json.loads(out)["data"]["repository"]["pullRequest"]
                 ["reviewThreads"]["nodes"])
    except (ValueError, KeyError, TypeError):
        return None, "review-thread query returned an unexpected shape"

    # An outdated thread hangs off a superseded diff; it does not gate a merge.
    return sum(1 for t in nodes
               if not t.get("isResolved") and not t.get("isOutdated")), None


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

    repo, na_reason = collect_repo(cwd)
    if repo is None:
        result.update(applicable=False, done=False, error=None)
        result["reason"] = na_reason
        return result, 2
    result["repo"] = repo

    b = Blockers()

    if repo["dirty"]:
        b.add("DIRTY_TREE", "uncommitted changes in the working tree",
              "commit or stash them")

    if repo["on_default"]:
        # Measured against origin/<base>, not @{u} — see collect_repo.
        if repo["ahead_of_default"]:
            b.add("UNPUSHED_COMMITS",
                  f"{repo['ahead_of_default']} commit(s) on "
                  f"{repo['branch']} not pushed to origin",
                  f"git push origin {repo['branch']}")
        elif repo["ahead_of_default"] is None:
            b.add("NOT_PUSHED",
                  f"could not compare against origin/{repo['default_branch']} "
                  "— is the branch pushed at all?",
                  f"git push -u origin {repo['branch']}")
    elif not repo["has_upstream"]:
        b.add("NOT_PUSHED", "branch has no upstream — never pushed",
              f"git push -u origin {repo['branch']}")
    elif repo["unpushed"]:
        b.add("UNPUSHED_COMMITS", f"{repo['unpushed']} commit(s) not pushed",
              "git push")

    # ---- direct-push ladder -------------------------------------------------
    # On the default branch there is no PR to open and no review to wait for.
    # Done is: committed, pushed, and level with origin. Stopping here also
    # keeps the check honest — demanding a PR that cannot exist would make the
    # gate unsatisfiable on exactly the repos where pushing to main is normal.
    if repo["on_default"]:
        if repo["behind"]:
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
            result.update(applicable=False, error=None)
            result["reason"] = "no GitHub remote — nothing to open a PR against"
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
        b.add("NOT_ON_DEFAULT",
              f"PR is merged but the repo is still on {repo['branch']}",
              f"git switch {repo['default_branch']} && "
              f"git pull --ff-only origin {repo['default_branch']}")
    elif (pr.get("state") or "").upper() == "CLOSED":
        b.add("PR_CLOSED",
              f"PR #{pr.get('number')} was closed without merging",
              "reopen it, or open a fresh PR — do not report this one as pending")
    else:
        if pr.get("isDraft"):
            b.add("PR_DRAFT", "pull request is a draft",
                  f"gh pr ready {pr.get('number')}")

        msg = NOT_MERGEABLE.get((pr.get("mergeStateStatus") or "").upper())
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

    if do_integrity:
        findings, warn = collect_integrity(repo["root"], repo["default_branch"])
        if warn:
            result["warnings"].append(warn)
        if findings:
            result["integrity"] = findings
            codes = ", ".join(sorted({f["code"] for f in findings}))
            b.add("INTEGRITY", f"diff weakens the test/CI signal: {codes}",
                  "revert the weakening, or justify the line with a `dod-ok` comment")

    result["blockers"] = b.as_list()
    result["done"] = len(b) == 0
    return result, (0 if result["done"] else 1)


# --------------------------------------------------------------------- output


def render(result):
    if not result["applicable"]:
        return f"dod-check: n/a — {result.get('reason', 'unknown')}"

    lines = []
    repo = result["repo"]
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


def main():
    ap = argparse.ArgumentParser(description="Is this branch's work done?")
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
