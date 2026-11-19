#!/usr/bin/env python3
"""PR-open gate: no pull request without an advice-refine-test-loop review.

WHY. The parent agent kept opening PRs without running the ARTL close-out
review. Prose in CLAUDE.md and the skill did not change that, so the PR-open
call itself now depends on evidence: advisor verdicts recorded by
hooks/artl-evidence.py, which the agent's own tools cannot write (see
artl_gate.py for where they live and how far that holds).

WHAT IT IS. Friction, not a security boundary: the goal is that an honest
agent reviews before it opens a PR, because the ordinary way of opening one
depends on a review. An agent set on evading it can (artl_gate.py,
ENFORCEMENT MODEL, lists the routes it does not reach: curl to the REST API,
a script file or git hook or gh alias, an interpreter, a variable spelling
such as `${g}h pr create`). The checks below are built against the paths an
honest agent stumbles into, not against those.

WHAT ENFORCES IT. This hook alone, by the user's choice. There is no
`permissions.ask` rule behind it: globs over the whole command string
prompted on commit messages, greps and paths that merely contained the
words. `Bash` and the GitHub MCP tools are allowed, so a pass (no output)
lets the call run, and a hook that times out, crashes, misparses or is not
installed lets it run too. That fail-open is accepted.

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
      4. the user waived it in this session with `skip-artl` (consumed by this
         one PR open, logged; see UserPromptSubmit).
    Fail -> permissionDecision "deny" with every reason. Pass -> no output.
    When the same Bash command runs `git commit` before the PR open, the gate
    (which runs first) also judges what that commit will add -- tracked edits,
    and untracked files too when the command runs `git add`.
    Anything the hook cannot pin down is a deny: a `cd` it cannot follow, a
    detached HEAD, a body it cannot read, another repository, two PR opens in
    one command, an earlier step that could move the branch, an unreachable
    remote, an environment in the open's own segment that points gh elsewhere
    (GH_HOST, GIT_DIR, GIT_WORK_TREE). `env -C DIR` is followed.

  UserPromptSubmit
    The user arms a one-shot override for this session by sending a message
    in which `skip-artl` stands alone on a line of its own (or is the whole
    message). A mention inside a sentence -- "what does skip-artl do?", or a
    pasted refusal from this gate, which names the word -- arms nothing.
    A prompt carrying any agent-authored wrapper never arms it: a
    task-notification (subagent and background-job output, a resumed
    advisor's reply), a `<cross-session-message>` from another Claude session,
    a teammate/agent/channel/coordinator message (AGENT_CHANNEL). MEASURED on
    2.1.322 with a live peer: the hook's `prompt` is the wrapped text and the
    payload has no other field naming the sender, so the wrapper is the only
    signal -- and the peer's body arrives with the word alone on its own line.
    No attempt is made to strip a wrapper and read the rest -- that is regex
    surgery on an agent's channel. A user who wants to waive sends a clean
    message.

Fail-open on unparseable input (exit 0, no output), per writing-hooks.
Test: tests/test_pr_open_gate.py
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import artl_gate as g

SKIP_TOKEN = "skip-artl"
# Text written by an agent, not the user, reaches UserPromptSubmit wrapped in
# one of these (Claude Code 2.1.322's own list of agent/untrusted wrappers,
# plus the framing lines it puts around a peer message). MEASURED for a peer
# SendMessage: prompt == '<cross-session-message from="uds:..." from-name=".."
# from-mode="prompting">\n<body>\n</cross-session-message>', and nothing else
# in the payload says who wrote it.
AGENT_CHANNEL = re.compile(
    r"task[-_ ]?notification"
    r"|<\s*(?:cross-session-message|teammate-message|agent-message|coordinator-relay"
    r"|remote-review|slack-ping|slack-tag-message|fetched-web-content"
    r"|untrusted-widget-state|tick>|channel\s)"
    r"|<<\s*autonomous-loop"
    r"|(?:Another Claude|A peer) session sent a message", re.I)

REMEDY = (
    "\nWhat clears it: the advice-refine-test-loop skill run on the finished diff "
    "(its advisor verdicts, and the files each advisor reads, are recorded "
    "automatically), its findings fixed, then the same PR open again, as its own "
    "command. Edits of up to 20 lines after that review still count. "
    "If the advisor is unavailable, only the user can waive the review: their own "
    "message with the waiver word (skip-artl) standing alone on a line of its own. "
    "The waiver covers one PR open and is logged."
)


def out(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()


def refuse(reasons: list[str], with_remedy: bool) -> None:
    text = "pr-open-gate refused this PR open:\n" + "\n".join(f"- {r}" for r in reasons)
    out({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                "permissionDecision": "deny",
                                "permissionDecisionReason": text + (REMEDY if with_remedy else "")}})


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
    if AGENT_CHANNEL.search(prompt) or \
            not any(line.strip() == SKIP_TOKEN for line in prompt.splitlines()):
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
    session = g.safe_id(payload.get("session_id")) or "unknown"
    if call.get("problem"):
        refuse([call["problem"]], with_remedy=False)
        return
    now = time.time()
    info, branch, tip, problem = locate(payload, call)
    if problem:
        refuse([problem], with_remedy=False)
        return
    top = info["top"]
    base, problem = g.remote_base(top)
    if problem:
        refuse([problem], with_remedy=False)
        return
    mb = g.git(top, "merge-base", base, tip)
    changes = g.numstat(top, mb, tip) if mb else None
    if changes is None:
        refuse([f"no merge base with the remote default branch could be found in {top}"],
               with_remedy=False)
        return

    smoke, smoke_log = g.smoke_problem(top, mb, tip, changes)
    if smoke:
        refuse([smoke], with_remedy=False)
        return

    where = {"repo": info["repo"], "top": top, "branch": branch, "head": tip}
    pending, problem = pending_commit_view(call, info, branch, mb)
    if problem:
        refuse([problem], with_remedy=False)
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
                refuse([problem], with_remedy=True)
                return
            log_exemption(payload, {"kind": "user-override", "why": problem, **where})

    if smoke_log:
        log_exemption(payload, {**smoke_log, **where})


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
    elif event == "UserPromptSubmit":
        on_prompt(payload)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        # A crash renders no decision, so the call runs: hook-only, by choice.
        sys.exit(0)
