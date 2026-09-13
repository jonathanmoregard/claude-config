# Task-scoped definition-of-done design

## Goal

Let one interactive session move from completed or intentionally parked work to
an independent task without stale repository state hijacking every later Stop.
Keep definition-of-done enforcement active across ordinary follow-up prompts and
through long-running work.

## Failure reproduced

Session `01a09580-9622-7113-9424-9edb585636ff` edited the NixOS worktree
`scheduled-job-reliability`. Its pull request later merged and the worktree was
removed. The session's armfile still contained that absolute path because a
clean Stop deliberately leaves armfiles in place to avoid a concurrent clean
measurement disarming a blocking sibling. Later, unrelated RSI prompts caused
the gate to call `dod-check.py` on the removed path. The checker correctly
failed closed, and the gate repeatedly blocked on work that no longer belonged
to the active task.

The checker is not the source of the bug. Current scope is one append-only set
per session, so “work touched sometime in this session” and “work owned by the
current task” are represented identically.

## Safety invariant

Task scope may change only because the interactive user explicitly declares a
new definition-of-done task. It must not change because:

- another user message arrived;
- elapsed time or repository inactivity crossed a threshold;
- a checker returned an inconvenient result;
- an assistant wrote a marker or closing envelope;
- a machine-generated prompt, scheduled wake, or subagent event appeared.

This preserves the leash for long work: “continue”, status questions,
corrections, compaction, and quota pauses remain inside the same scope. A worker
editing after an authorized reset re-arms its repository in the new scope.

## User contract

Canonical syntax is deliberately plain text, not an unregistered slash command
that a client may consume before `UserPromptSubmit`:

```text
Reset DoD to new task: <non-empty task description>
```

For natural continuity with the motivating request, a case-insensitive prompt
starting with `reset dod [skill] to [a] new task:` and a non-empty description
has the same meaning. Matching is anchored at the beginning and requires the
description. Generic phrases such as “new task”, “what is next?”, and
“continue” never reset scope.

The hook acts only on `UserPromptSubmit` and requires exact `source: user`.
Known synthetic sources and source-less legacy payloads are ignored. Logging
hooks may accept source-less events for compatibility; an enforcement reset
must fail closed when human authority is not identified.

## State transition

Add `hooks/finishing-up-task-scope.py` as a `UserPromptSubmit` hook. On an
accepted directive it:

1. validates the session token and task description;
2. refuses the transition if Ralph's live `<session>.md` ownership record
   exists;
3. takes a stable `$CLAUDE_DOD_STATE_DIR/.task-scope.lock` advisory lock;
4. reads and de-duplicates current `<session>.arm` repositories;
5. appends one compact `task-reset` audit record to `gate.log` with session,
   prompt id, and retired repository paths, but not prompt prose;
6. unlinks `<session>.blocks` first, then `<session>.arm`.

Resetting the counter first is fail-closed. If counter removal fails, active
scope remains untouched. If arm removal then fails, old scope remains active
with a fresh retry budget. Any failure emits additional context and leaves the
agent aware that the task boundary did not take effect.

`finishing-up-arm.sh` takes the same stable lock around its existing locked
set append. Thus an edit is wholly before or after reset, never erased by a
check-then-delete race. The stable lock file is shared across sessions and
contains no task data; it cannot become a per-session leak needing reaping.

Successful reset emits short additional context stating how many prior
repositories were retired. No armfile is created for the new task until an edit
lands, preserving the existing zero-cost behavior for read-only tasks.

## Long-running ownership

Ralph already owns a session through a runtime state file and the Stop gate
yields to it. Reset refuses while that file exists. User must halt or complete
the loop first, keeping one driver and one task boundary.

Other background workers have no trusted task identifier. They therefore do
not receive an exemption: any later repository edit runs the arm hook and joins
the active scope. Explicit user reset can retire already-landed work, but it
cannot stop subsequent work from re-arming.

## Options considered

| Option | Decision | Reason |
|---|---|---|
| Reset on every user prompt | Reject | A routine “continue” or status question releases long-running work. |
| Infer task changes from prompt meaning | Reject | Classifier errors become silent enforcement bypasses. |
| Retire repositories whenever they measure clean | Defer | Useful cleanup, but removed worktrees can vanish before a clean measurement and this does not represent user task ownership. |
| Explicit user-authenticated task boundary | Adopt | Narrow authority, deterministic behavior, session isolation, and no agent-selected escape based on checker state. |

## Boundaries

No changes to `dod-check.py`, blocker semantics, PR/CI policy, merge authority,
Ship/Preserve/Abandon outcomes, block budget, generated `~/.codex`, or another
session's state. Claude source reaches Codex through existing sync.

## Verification

- Parser cases for canonical and motivating syntax, required description,
  anchoring, case handling, and explicit machine sources.
- State cases proving current session arm/count reset, deduplicated audit,
  other-session isolation, empty-state idempotence, and visible failures.
- Ralph ownership case proving reset refusal and unchanged state.
- Serialization case proving reset waits behind arming's stable lock and cannot
  erase a post-boundary edit.
- Existing full Stop-hook interaction suite, Python compile, Bash syntax,
  settings JSON parse, skill checks, and `git diff --check`.
