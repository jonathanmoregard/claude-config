---
name: finishing-up
description: Use when wrapping up work that produced a diff; before asking the user to merge, review, or click a PR; when deciding whether unfinished work should ship, be preserved, or be abandoned; when CI, checks, mergeability, or unpushed work block completion; or when the user asks to reset DoD to a new task.
---

# Finishing Up

Choose the terminal outcome before acting on Git blockers. `dod-check` measures
shipping readiness; it does not decide whether work should ship.

**Iron law: never report repository or PR state you have not just measured.**

## Explicit task boundaries

One session has one active DoD scope. To retire completed or intentionally
parked repositories before unrelated work, the interactive user starts a prompt
with:

```text
Reset DoD to new task: <non-empty task description>
```

`Reset DoD skill to new task: …` is accepted too. The `UserPromptSubmit` hook
applies the boundary before the turn begins, resets only that session's arm and
retry state, and audits retired repository paths. It does not ship, preserve,
abandon, or delete repository work.

Never invoke the task-scope hook yourself or edit its runtime state to escape a
checker result. Generic “new task”, “next”, continuation, correction, status,
compaction, and quota-pause prompts remain inside the existing scope. Only a
runtime user event whose session id, prompt id, and text match the latest typed
prompt in a trusted Claude or Codex transcript can reset it. Each event is
consumed once; synthetic, source-less, untrusted-path, mismatch, and replay
events fail closed. Other sessions are unaffected.

Reset is refused while Ralph owns the session. `UserPromptSubmit` does not
expose generic background-task ownership, so typed user authority defines that
boundary. Any repository edit that lands after a successful reset arms the new
task normally, including edits from a worker that outlives the boundary.

## Step 1 — Select the outcome

| Outcome | Use when | Action |
|---|---|---|
| **Ship** | Work belongs upstream and no contrary user intent exists. | Complete the delivery ladder below. |
| **Preserve** | The user explicitly wants work retained for later, or another session owns it. | Do not publish or alter it. Record exact worktree, branch, HEAD, measured blockers, reason, and next owner/action. |
| **Abandon** | The user explicitly says the work has no value or authorizes discard. | Resolve exact targets and ownership, then remove only the authorized work. Report what was removed and whether recovery is possible. |

Never push or open a PR solely to satisfy the gate. Deletion authorization is
defined only in global `CLAUDE.md` under **Safe cleanup without asking** and
**Worktrees**. Apply that policy without restating or broadening it here.
Existing approval such as “discard that worktree” is sufficient; do not ask
twice. If Ship is doubtful and neither Preserve nor Abandon is authorized, ask
one focused outcome question.

Preserve is an intentional handoff, not a claim that shipping is complete.
Abandon ends by measuring the remaining/default checkout, not the deleted path.

### When the DoD Stop hook asks for an explanation

Ship has no override: clear its measured blockers. For another terminal outcome,
explain it normally, then end with these two lines:

```text
DOD-EXIT: {"outcomes":[<one record per blocked repo>]}
<state marker>
```

Use exact uppercase outcome values and exact fields:

| Outcome | Record fields | Final marker |
|---|---|---|
| `QUESTION` | `repo,outcome,question` | `QUESTION` |
| `BLOCKED` | `repo,outcome,blocker,needed` | `BLOCKED` |
| `PRESERVE` | `repo,outcome,reason,owner,next` | `DONE` |
| `ABANDON` | `repo,outcome,reason,remaining_work` | `DONE` |

JSON stays on one line; all values are non-empty strings; `repo` is the exact
absolute path from the nudge; a question ends in `?`. Cover every blocked repo
exactly once. With mixed outcomes, `QUESTION` wins over `BLOCKED`; otherwise
use `DONE`. `ABANDON` explains why work should not ship but never authorizes
deletion. This ends only the current Stop chain; a later user turn measures
again.

## Step 2 — Measure

Run fresh in every repo this work touched:

```bash
python3 ~/.claude/scripts/dod-check.py --repo <repo>
python3 ~/.claude/scripts/dod-check.py --repo <repo> --json
```

Exit codes: `0` done · `1` blockers · `2` not applicable · `3` unknown.
Treat `3` as not done. Use `gh` for current PR state; memory is not evidence.

For Preserve, report the measured blockers without applying Ship fixes. For
Abandon, measure before resolving targets and again after authorized cleanup.
Only Ship interprets delivery blockers as commands to publish.

## Step 3 — Ship

`dod-check` selects the ladder:

- **Default branch:** committed → pushed → level with `origin`.
- **Feature branch:** committed → pushed → PR open → checks green (or repo has
  no CI) → mergeable → Jonathan's merge click → switch the checkout to the
  default branch and pull.

The merge click, credentials, interactive logins, and taste decisions are human
actions. Everything else is yours. Work blockers in reported order:

| Blocker | Action |
|---|---|
| `DIRTY_TREE` | Commit the intended work. |
| `NOT_PUSHED` / `UNPUSHED_COMMITS` | Push. On default branch compare with `origin/<base>`. |
| `BEHIND_REMOTE` | `git pull --ff-only`. |
| `NO_PR` | Create a PR and write an accurate body. |
| `PR_DRAFT` | Mark it ready. |
| `CHECKS_PENDING` | Schedule one durable reporting follow-up or keep one background watch. Never spin. |
| `CHECKS_FAILING` | Attribute against base; fix only branch-caused failures. Invoke `hardening-flaky-ci` for proven flakes. |
| `NOT_MERGEABLE` | Update from base, resolve conflicts, or name the unmet protection. |
| `UNRESOLVED_THREADS` | Answer and resolve each thread. |
| `NO_CHECKS` | If workflows exist, find why none ran. No CI at all is only a warning. |
| `INTEGRITY` | Restore weakened signal; use `dod-ok` only when genuinely correct and reviewable in the diff. |
| `PR_CLOSED` | Do not report it as pending. |
| `NOT_ON_DEFAULT` | Switch to default and pull after merge. |

`MERGED` never means ask for another click. Several repos get separate
measurements and separate outcome decisions. Subagent edits remain parent-owned.

### Pending checks

Create one durable, one-shot follow-up, delayed to the pipeline's normal
duration and scheduled off-minute. Its self-contained prompt includes repo,
branch, PR, a fresh `dod-check`, and actions for green/red/still-pending.
It reports only: it must not merge or push work the user has not seen. A durable
follow-up does not clear the current Stop gate.

### Red checks

Test the same failure on the base commit:

- Base fails too: pre-existing; report it, do not mutate this diff.
- Branch only: inspect failed logs and fix the cause.
- Identical code passes on rerun: treat as flaky and harden it.

Never weaken a check. Stop after three identical failed attempts and report the
retry evidence.

## Step 4 — Handover

### Ship

Require fresh `dod-check` exit `0`. Give:

1. Executive summary of behavior and tooling/config changes.
2. Visual-review URL and stop command when rendered output changed.
3. Full clickable PR URLs from the fresh checker, with branch, base, and commit
   count. Put PR links at the bottom.

State the only remaining human action. If it is the merge click, also state the
measured green and mergeable status.

### Preserve

Give exact worktree, branch, HEAD, measured blockers, why it is retained, and
who/what resumes it. Include a PR URL only when one exists. Do not describe the
work as ready or done.

### Abandon

Give exact removed worktree/branch, recovery status, and fresh state of the
remaining/default checkout. Include no PR section unless a PR actually exists.

## Red flags

- Choosing Ship because `NO_PR` exists rather than because work belongs upstream
- Pushing rejected, exploratory, or accidental work to silence the gate
- Deleting outside the authorization in global `CLAUDE.md`, or touching
  another session's branch
- Claiming ready/merged/blocked from stale PR state
- Rerunning unchanged pending checks instead of scheduling one follow-up
- Treating exit `3`, `NO_CHECKS`, or a hook timeout as green

## Scope

Any work that produced a diff. Not for read-only questions or research with no
repository change.
