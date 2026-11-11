#!/usr/bin/env python3
"""PR-open gate: no pull request without an advice-refine-test-loop review.

WHY. The parent agent kept opening PRs without running the ARTL close-out
review. Prose in CLAUDE.md and the skill did not change that, so the PR-open
call itself now depends on evidence: advisor verdicts recorded by
hooks/artl-evidence.py, which the agent's own tools cannot write (see
artl_gate.py for where they live and how far that holds).

WHAT ENFORCES IT. Not this hook. settings.json `permissions.ask` covers every
PR-open route (artl_gate.py, ENFORCEMENT MODEL), and with `Bash` allowed it
is that rule which stops an unreviewed open: this hook timing out, crashing,
misparsing a command or not being installed all leave the prompt in place.
This hook's privilege is to ANSWER the prompt "allow" -- once, for the exact
call it judged -- and to deny with a remedy. Nothing depends on the deny.

EVENTS (one script, dispatched on hook_event_name):

  PreToolUse (Bash | mcp__github__create_pull_request)
    For `gh pr create` / `gh pr new`, a POST to `gh api .../pulls`, a GraphQL
    createPullRequest, and the GitHub MCP create tool. Everything else exits 0
    silently. A PR open passes when, for the branch it opens (its --head /
    head= when named, else the checkout's current branch), against the base
    the REMOTE reports:
      1. nixos-config only: a change touching services, scripts or GUI modules
         has an `Interactive smoke (nixos-agent-testing)` checklist field in
         its HEAD commit that names a file existing on disk, or `n/a: <reason>`
         (logged). Whether a VM run was NEEDED stays a human judgement; the
         hook only insists the claim points at something real.
      2. the diff since the merge base is computed-exempt (inert docs only, or
         at most 15 changed lines with nothing in a behaviour path) -- logged; OR
      3. the latest advisor verdict recorded for the branch is PASS or
         PASS_WITH_FIXES and not from a suspect prompt, every changed file's
         latest review (by an advisor that READ it) passed and has drifted at
         most 20 lines since, and the whole tree at most 20 lines; OR
      4. the user typed `skip-artl` in this session (consumed by this one PR
         open, logged).
    Fail -> permissionDecision "deny" with every reason. Pass -> no output, and
    a one-shot grant for the PermissionRequest below.
    When the same Bash command runs `git commit` before the PR open, the gate
    (which runs first) also judges what that commit will add -- tracked edits,
    and untracked files too when the command runs `git add`.
    Anything the hook cannot pin down is a deny and no grant: a `cd` it cannot
    follow, a detached HEAD, a body it cannot read, another repository, two PR
    opens in one command, an earlier step that could move the branch, an
    unreachable remote.

  PermissionRequest (same matcher)
    Fires because permissions.ask matched. When the gate passed this exact
    call in this session within GRANT_TTL_S, the request is answered "allow"
    and the grant is spent. No grant -> no output -> the ordinary prompt.
    Measured on Claude Code 2.1.322: a PreToolUse "allow" does NOT lift an ask
    rule, so without this the user would be prompted for every PR.

  UserPromptSubmit
    The user typing the token `skip-artl` arms a one-shot override for this
    session. A prompt carrying any `task-notification` marker never arms it:
    subagent and background-job output arrives as a user turn wrapped in that
    tag (measured, including a resumed advisor's reply), and its contents are
    written by the very agents the gate constrains. No attempt is made to strip
    the tag and read the rest -- that is regex surgery on an adversary's
    channel. A user who wants to waive sends a clean message.

Fail-open on unparseable input (exit 0, no output), per writing-hooks; the
ask rule covers the gate itself failing. Test: tests/test_pr_open_gate.py
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import artl_gate as g

SKIP_TOKEN = re.compile(r"(?<![\w-])skip-artl(?![\w-])")
NOTIFICATION_MARK = re.compile(r"task[-_ ]?notification", re.I)

REMEDY = (
    "\nWhat clears it: the advice-refine-test-loop skill run on the finished diff "
    "(its advisor verdicts, and the files each advisor reads, are recorded "
    "automatically), its findings fixed, then the same PR open again, as its own "
    "command. Edits of up to 20 lines after that review still count. "
    "If the advisor is unavailable, only the user can waive the review, by typing "
    "skip-artl in a message; the waiver covers one PR open and is logged."
)


def out(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()


def deny(reasons: list[str], with_remedy: bool) -> None:
    text = "pr-open-gate refused this PR open:\n" + "\n".join(f"- {r}" for r in reasons)
    out({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                "permissionDecision": "deny",
                                "permissionDecisionReason": text + (REMEDY if with_remedy else "")}})


def grant_path(session: str, key: str) -> Path:
    return g.state_dir() / "grants" / f"{session}.{key}.json"


def override_path(session: str) -> Path:
    return g.state_dir() / "override" / f"{session}.json"


def log_exemption(payload: dict, entry: dict) -> None:
    g.append_jsonl(g.session_records(payload) / g.EXEMPTIONS_FILE,
                   {"ts": time.time(), "session": payload.get("session_id"), **entry})


# --- UserPromptSubmit ---------------------------------------------------------

def on_prompt(payload: dict) -> None:
    session = g.safe_id(payload.get("session_id"))
    prompt = payload.get("prompt")
    if not session or not isinstance(prompt, str):
        return
    if NOTIFICATION_MARK.search(prompt) or not SKIP_TOKEN.search(prompt):
        return
    g.write_json(override_path(session), {"ts": time.time(), "prompt_sha256": g.sha256(prompt)})
    out({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": (
        "pr-open-gate: the user typed skip-artl. The next PR open in this session that "
        "lacks advice-refine-test-loop evidence is allowed once, and the waiver is logged "
        "to artl-exemptions.jsonl.")}})


def take_override(session: str, now: float) -> bool:
    path = override_path(session)
    data = g.read_json(path)
    if not data:
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return now - float(data.get("ts", 0)) <= g.OVERRIDE_TTL_S


# --- PermissionRequest --------------------------------------------------------

def on_permission(payload: dict) -> None:
    session = g.safe_id(payload.get("session_id"))
    tool, tool_input = payload.get("tool_name"), payload.get("tool_input")
    if not session or g.pr_open_call(tool, tool_input) is None:
        return
    path = grant_path(session, g.call_key(tool, tool_input))
    data = g.read_json(path)
    if not data:
        return
    try:
        path.unlink()
    except OSError:
        return
    if time.time() - float(data.get("ts", 0)) > g.GRANT_TTL_S:
        return
    out({"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                "decision": {"behavior": "allow"}}})


# --- PreToolUse ---------------------------------------------------------------

def call_dir(payload: dict, call: dict) -> str:
    base_cwd = payload.get("cwd") or "."
    cwd = call.get("cwd")
    if not cwd:
        return base_cwd
    p = Path(cwd).expanduser()
    return str(p if p.is_absolute() else Path(base_cwd) / p)


def input_head(cwd: str, name: str) -> tuple[str | None, str]:
    """The head of a `gh api --input FILE` body."""
    p = Path(name).expanduser()
    p = p if p.is_absolute() else Path(cwd) / p
    try:
        body = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"cannot read the request body {str(p)!r} ({exc.__class__.__name__})"
    head = body.get("head") if isinstance(body, dict) else None
    if not isinstance(head, str) or not head:
        return None, f"the request body {str(p)!r} names no head branch"
    return head, ""


def locate(payload: dict, call: dict) -> tuple[dict | None, str | None, str | None, str]:
    """(repo info, branch, tip, problem) for the branch this call opens."""
    cwd = call_dir(payload, call)
    if not Path(cwd).is_dir():
        return None, None, None, f"cannot inspect {cwd!r}: not a directory this hook can follow"
    info = g.repo_info(cwd)
    if not info:
        return None, None, None, f"{cwd} is not inside a git checkout, so the branch cannot be checked"
    top = info["top"]
    head = call.get("head")
    if call.get("input"):
        head, problem = input_head(cwd, call["input"])
        if problem:
            return info, None, None, problem
    problem = g.target_problem(top, call.get("repo"), head)
    if problem:
        return info, None, None, problem
    if isinstance(head, str) and head:
        branch = head.split(":", 1)[-1]
        tip = (g.git(top, "rev-parse", "--verify", "-q", f"refs/heads/{branch}^{{commit}}")
               or g.git(top, "rev-parse", "--verify", "-q", f"refs/remotes/origin/{branch}^{{commit}}"))
        if not tip:
            return info, branch, None, f"branch {branch!r} is not in the checkout at {top}"
        return info, branch, tip, ""
    branch = g.current_branch(top)
    if not branch:
        return info, None, None, f"{top} has a detached HEAD and the PR open names no head"
    tip = g.git(top, "rev-parse", "HEAD")
    return info, branch, tip, "" if tip else f"cannot resolve HEAD in {top}"


def pending_commit_view(call: dict, info: dict, branch: str,
                        mb: str) -> tuple[list[tuple[str, list]], str]:
    """A second view for `git commit ... && gh pr create`: the gate runs before
    that commit, so the committed tip still looks reviewed. Measured in e2e.
    The view is the working tree on top of the tip: tracked edits (staged or
    not), plus untracked files when the command runs `git add` -- the only way
    an untracked file reaches that commit (finding 7). ([views], problem)."""
    top = info["top"]
    if not call.get("commits") or g.current_branch(top) != branch:
        return [], ""
    inside = [w for w in call.get("writes") or []
              if not w.startswith("/") or (w + "/").startswith(top.rstrip("/") + "/")]
    if inside:
        return [], (f"this command writes {inside[0]!r} inside the checkout before "
                    f"committing, so what it commits cannot be judged in advance")
    work = g.snapshot_tree(top, untracked=bool(call.get("adds")))
    rows = g.numstat(top, mb, work) if work else None
    if rows is None:
        return [], "cannot snapshot the working tree that this command commits"
    return [(work, rows)], ""


def on_pretool(payload: dict) -> None:
    tool, tool_input = payload.get("tool_name"), payload.get("tool_input")
    call = g.pr_open_call(tool, tool_input)
    if call is None:
        return
    if call.get("problem"):
        deny([call["problem"]], with_remedy=False)
        return
    session = g.safe_id(payload.get("session_id")) or "unknown"
    now = time.time()
    info, branch, tip, problem = locate(payload, call)
    if problem:
        deny([problem], with_remedy=False)
        return
    top = info["top"]
    base, problem = g.remote_base(top)
    if problem:
        deny([problem], with_remedy=False)
        return
    mb = g.git(top, "merge-base", base, tip)
    changes = g.numstat(top, mb, tip) if mb else None
    if changes is None:
        deny([f"no merge base with the remote default branch could be found in {top}"],
             with_remedy=False)
        return

    smoke, smoke_log = g.smoke_problem(top, mb, tip, changes)
    if smoke:
        deny([smoke], with_remedy=False)
        return

    where = {"repo": info["repo"], "top": top, "branch": branch, "head": tip}
    pending, problem = pending_commit_view(call, info, branch, mb)
    if problem:
        deny([problem], with_remedy=False)
        return
    views = [(tip, changes)] + pending
    exempt = None
    for _, view_changes in views:
        exempt = g.computed_exemption(view_changes)
        if not exempt:
            break
    if exempt:
        log_exemption(payload, {"kind": "computed", "why": exempt, **where})
    else:
        records: list[dict] = []
        for d in g.read_records_dirs(payload, info):
            records.extend(g.read_jsonl(d / g.EVIDENCE_FILE))
        problem = None
        for view_tip, view_changes in views:
            problem = g.evidence_problem(records, info, branch, view_tip, view_changes)
            if problem:
                break
        if problem:
            if not take_override(session, now):
                deny([problem], with_remedy=True)
                return
            log_exemption(payload, {"kind": "user-override", "why": problem, **where})

    if smoke_log:
        log_exemption(payload, {**smoke_log, **where})
    stale = g.prune(g.state_dir() / "grants", g.GRANT_TTL_S, now)
    if stale:
        print(f"pr-open-gate: could not remove {stale} expired grant file(s); "
              "they are already unspendable, but the state dir is not writable "
              "as expected", file=sys.stderr)
    g.write_json(grant_path(session, g.call_key(tool, tool_input)), {"ts": now, **where})


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (OSError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0
    event = payload.get("hook_event_name")
    if event == "PreToolUse":
        on_pretool(payload)
    elif event == "PermissionRequest":
        on_permission(payload)
    elif event == "UserPromptSubmit":
        on_prompt(payload)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        # A crash renders no decision; the permissions.ask rule then prompts.
        sys.exit(0)
