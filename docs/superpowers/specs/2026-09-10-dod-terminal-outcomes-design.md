# DoD terminal outcomes design

## Goal

Keep the definition-of-done gate useful without making “publish a pull
request” the only way a session may end. Continue nudging until the agent
either clears the objective blockers or gives the user one explicit,
machine-checkable terminal explanation.

## Failure reproduced

`dod-check.py` correctly reported seven unpushed commits and no PR in a
worktree containing evaluation evidence for a rejected proposal. The Stop hook
then ordered the agent to “drive to green and mergeable.” The current
`finishing-up` skill maps `UNPUSHED_COMMITS` to push and `NO_PR` to create a PR,
with no rejected or intentionally preserved outcome. A fresh baseline agent
therefore chose to publish the rejected work.

The same Stop decision was injected six times. Both Claude Code and Codex expose
`stop_hook_active`, a runtime-owned boolean meaning the turn has already been
continued by a Stop hook. It distinguishes a reply to this gate from a stale
assistant message on a later user turn, but it does not prove that the reply
resolved or explained the unfinished state.

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
2. If unfinished, issue concise guidance that names Ship, Preserve, Abandon,
   Question, and Blocked; forbids PR creation solely to clear the gate; and
   forbids deletion without user approval.
3. On runtime-authenticated ordinary re-entry (`stop_hook_active is True`),
   accept only this closing envelope in `last_assistant_message`:

   ```text
   DOD-EXIT: {"outcomes":[{"repo":"/absolute/path","outcome":"QUESTION","question":"Which outcome should I use?"}]}
   QUESTION
   ```

   The JSON is one line and has only an `outcomes` array. Each currently blocked
   repository must appear exactly once by the absolute path printed in the
   nudge. Unknown, duplicate, missing, or already-clean repositories reject the
   whole envelope. Each record has exact keys for its outcome:

   | Outcome | Required keys after `repo`, `outcome` | Meaning |
   |---|---|---|
   | `QUESTION` | `question` ending in `?` | One user decision is required. |
   | `BLOCKED` | `blocker`, `needed` | An external or human action is required. |
   | `PRESERVE` | `reason`, `owner`, `next` | Work is intentionally retained and handed off. |
   | `ABANDON` | `reason`, `remaining_work` | Work should not ship; report what remains. This grants no deletion authority. |

   Required values are non-empty strings. Natural-language explanation comes
   before the envelope; the structured fields keep the same facts visible and
   checkable. The aggregate final marker is `QUESTION` if any record asks a
   question, otherwise `BLOCKED` if any record is blocked, otherwise `DONE`.
   The gate can validate structure and repository coverage, not the truth of an
   agent's explanation; visibility lets the user challenge a false claim.
4. If the closing envelope is absent or malformed, measure again and continue
   nudging while blockers remain. A clean objective measurement still ends the
   gate without an envelope. The existing block budget bounds a non-compliant
   client below the platform's hard loop cap.
5. Accept an envelope only on the current runtime-authenticated re-entry. Do not
   persist it, delete the armfile, or trust it on the next user turn. Reset the
   current block count after an accepted explanation so a later turn gets a
   fresh bounded check. Failure to reset rejects the exit and blocks with the
   reset error; it must never leave the next turn at a spent budget.
6. Do not trust truthy strings or objects for `stop_hook_active` or non-string
   `last_assistant_message`. An explicit Ralph handover always measures: it is
   the loop's last delivery check, not an ordinary recursive Stop.
7. Keep background-task, scheduled-follow-up, Ralph ownership, objective
   checker, and fail-closed behavior unchanged.

For a running external check, the continuation permits one durable reporting
follow-up and forbids that follow-up from merging or pushing unseen work. Human
blockers must report measured facts; a PR URL is required only when a PR exists.

## Options considered

| Option | Decision | Reason |
|---|---|---|
| Lower six-block budget globally | Reject | Saves tokens by weakening every runtime, including ones without re-entry metadata. |
| Durable agent-written outcome marker | Reject | A persistent self-attested waiver becomes an unfenceable lease and can suppress later independent checks. |
| Infer outcome from transcript prose | Reject | Transcript is asynchronous and unstable; agent text is not user authority. |
| Copy ignored state into new worktrees | Reject | Can leak credentials/runtime state and does not solve Stop semantics. |
| Allow every runtime re-entry | Reject | Saves tokens but lets a content-free reply end enforcement immediately. |
| Runtime-scoped closing envelope | Adopt | Uses synchronous final text plus runtime re-entry state, exposes the reason to the user, does not survive the current Stop chain, and leaves objective Git measurement unchanged. |

## Boundaries

No changes to `dod-check.py`, settings registration, destructive commands,
blocker detection, CI policy, merge authority, or generated `~/.codex` files.
The change lives in Claude source and reaches Codex through existing sync.

## Verification

- RED/GREEN tests for all four outcome schemas, mixed aggregate markers,
  complete multi-repository coverage, unknown/duplicate/missing repositories,
  malformed JSON and records, `stop_hook_active: true`/false/absent/wrong shape,
  non-string final messages, later-turn replay, reset failure, and a handover
  case that must still measure.
- Test harness supplies the worktree gate, Ralph hook, state resolver, and
  checker through an isolated fake home; handover integration never reaches
  the live installed copy.
- Message-contract tests for all three outcomes and safety wording.
- Full Stop-hook interaction suite and Python syntax check.
- Baseline pressure scenario rerun against updated skill.
- Fresh-context adversarial review of integrated diff.
