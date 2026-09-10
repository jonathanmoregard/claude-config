# DoD terminal outcomes design

## Goal

Keep the definition-of-done gate useful without making “publish a pull
request” the only way a session may end. Prevent repeated Stop-hook feedback
from consuming turns and context after the agent has already received one
corrective continuation.

## Failure reproduced

`dod-check.py` correctly reported seven unpushed commits and no PR in a
worktree containing evaluation evidence for a rejected proposal. The Stop hook
then ordered the agent to “drive to green and mergeable.” The current
`finishing-up` skill maps `UNPUSHED_COMMITS` to push and `NO_PR` to create a PR,
with no rejected or intentionally preserved outcome. A fresh baseline agent
therefore chose to publish the rejected work.

The same Stop decision was injected six times. Both Claude Code and Codex expose
`stop_hook_active`, a runtime-owned boolean meaning the turn has already been
continued by a Stop hook. Current hook-authoring guidance says to return on
re-entry; `finishing-up-gate.py` ignores the field and instead spends a six-turn
fallback budget.

## Outcome model

Git state and user intent answer different questions:

- **Ship** — work belongs upstream. Run the existing delivery ladder: commit,
  push, open/update PR, reach green and mergeable, then wait for the protected
  merge click.
- **Preserve** — user explicitly wants unfinished work retained for later or
  another owner. Do not push or open a PR merely to satisfy the gate. Report
  exact branch, worktree, commit, and measured blockers.
- **Abandon** — work has no value. Delete only after explicit user authority,
  resolving exact targets first; then verify the remaining/default checkout.

`dod-check.py` remains an objective *shipping-readiness* measurement. It does
not infer whether work should ship. The `finishing-up` skill selects the
outcome from explicit user intent and task evidence before acting on checker
codes. If intent is unclear and shipping would publish rejected, exploratory,
or accidental work, ask one focused outcome question.

## Stop-hook behavior

1. On an armed initial Stop (`stop_hook_active` is false or absent), measure all
   existing Git state exactly as today.
2. If unfinished, issue one concise continuation that names Ship, Preserve,
   and Abandon, forbids PR creation solely to clear the gate, and forbids
   deletion without user approval.
3. On runtime-authenticated ordinary re-entry (`stop_hook_active is True`),
   allow immediately and log why. Do not trust truthy strings or objects. An
   explicit Ralph handover still measures: it is the loop's last delivery
   check, not a recursive continuation.
4. Retain the current block budget as a compatibility fallback for runtimes
   that omit the field and for new, independently initiated Stop attempts.
5. Keep background-task, scheduled-follow-up, Ralph handover, objective checker,
   and fail-closed behavior unchanged.

For a running external check, the continuation permits one durable reporting
follow-up and forbids that follow-up from merging or pushing unseen work. Human
blockers must report measured facts; a PR URL is required only when a PR exists.

## Options considered

| Option | Decision | Reason |
|---|---|---|
| Lower six-block budget globally | Reject | Saves tokens by weakening every runtime, including ones without re-entry metadata. |
| Agent-written outcome/handover marker | Reject | Self-attestable; prior marker became an unfenceable lease with race failures. |
| Infer outcome from transcript prose | Reject | Transcript is asynchronous and unstable; agent text is not user authority. |
| Copy ignored state into new worktrees | Reject | Can leak credentials/runtime state and does not solve Stop semantics. |
| Runtime re-entry guard + explicit outcomes | Adopt | Uses authoritative event state, bounds repetition, and keeps intent separate from Git measurement. |

## Boundaries

No changes to `dod-check.py`, settings registration, destructive commands,
blocker detection, CI policy, merge authority, or generated `~/.codex` files.
The change lives in Claude source and reaches Codex through existing sync.

## Verification

- RED/GREEN tests for `stop_hook_active: true`, false, absent, and wrong-shaped
  values, plus a handover case that must still measure.
- Test harness explicitly targets the worktree hook, never the live installed
  copy.
- Message-contract tests for all three outcomes and safety wording.
- Full Stop-hook interaction suite and Python syntax check.
- Baseline pressure scenario rerun against updated skill.
- Fresh-context adversarial review of integrated diff.
