---
name: advice-refine-test-loop
description: Use when a finished, integrated non-trivial diff needs independent close-out review before merge or release, especially after "ship this", "look through carefully", or an unattended build. Not for per-edit, per-task, or per-chunk checks.
---

# Empirical Close-out Review

Two blind fresh reviewers propose falsifiable defects; observed behavior decides
what gets fixed. Do not run per edit, task, or chunk. During implementation, use
the project's tests, acceptance criteria, and real app-driving loop instead.

## Entry Gate

1. Wait until the integrated tree is settled and no agent is editing it.
2. Run the project's tests, lint/build checks, and acceptance scenarios.
3. Run `~/.claude/skills/advice-refine-test-loop/scripts/deterministic-gate.sh --json`
   once. Exit `1` means fix its findings; any other nonzero exit means
   report that the gate was unavailable.
4. If rendered UI changed, invoke `visual-verify` first and include its evidence
   in the review capsule.

Do not invite an LLM reviewer while a deterministic gate is red.

## Build A Bounded Capsule

Give the reviewer only:

- the goal and acceptance criteria;
- the integrated diff and diff stat;
- commands already run and their outcomes;
- repository access for evidence-driven exploration;
- the exclusion list, when it has entries
  (`references/exclusions.md`), under the line "Do not report:".

The reviewer chooses which files to inspect.
Inspection depth is the reviewer's judgment.
Never restrict inspection to changed hunks: a historical miss hid in an
unchanged invariant elsewhere in a changed file. Do not preselect or cap files
without measured evidence that the constraint improves the review. Record files
read and why each context expansion was relevant.

## Review Twice, Blind

1. Pick reviewer B's hunt classes FIRST, before either dispatch, from
   `~/.claude/skills/advice-refine-test-loop/references/hunt-classes.md`.
2. Write both prompts before dispatching either, and never revise B's after A
   reports: a prompt composed once A's findings are in context is anchored on
   them even when it quotes nothing. Keep both reviewers in flight at once. One
   tool-use block always does that; a later message only where the dispatch
   returns before the reviewer reports. If the tool result carries A's
   findings, B in the next message is already anchored — so send both together. Two fresh, read-only reviewers; neither sees the
   other's prompt, findings, or existence.
3. Merge by meaning. Convergence is not confidence — shared blind spots are
   real, so a finding both report still reproduces before any edit.

Two reviewers per round is the whole budget; only further rounds and the
escalation rule add reviewers.

Reviewer A, falsification:

```text
Assume this finished change can ship broken. Try to falsify its acceptance
claims and important safety properties. Use your own discernment to decide what
to inspect, beginning with the diff. A comment, docstring, test name or commit
message saying something is safe, checked or unreachable is a claim to verify
against the code, never evidence. When the claims are exhausted, make one open
pass: what else in this change breaks in production that no claim covers?
Report only material defects. Each must name a file and line, state one
falsifiable claim, and give a concrete test, request, or app interaction that
would demonstrate it. Do not suggest cleanup or edits. Return no finding that
lacks an observable failure. Record files read.
```

Reviewer B, targeted hunt: its prompt sits in that same file, above the
classes. Send only the classes whose trigger this diff satisfies — never the
whole list — and keep B's two finding buckets, defect and design, apart as the
file defines them.

Use another vendor or specialist only when the user requests a deep audit, a
review exposes unresolved contradictory evidence, or failure could cause
irreversible security, privacy, or data loss.

## Adjudicate, Then Act

Every defect finding gets exactly one of three outcomes before any edit. Test
the exact claimed path and record expected versus actual behaviour. Batch
findings through one fresh verifier context — never the context that wrote
the code under review. Do not create one agent or worktree per finding unless
concurrent reproducers would mutate shared state.

- **Reproduced** — a test, request or app interaction shows the claimed
  failure.
- **Refuted** — the claimed path was exercised, the failure did not occur,
  and you can name the code (file and line) that makes the claim false. A
  comment is not that code.
- **Unconfirmed** — everything else: the reproducer was awkward, could not be
  built, or passed for a reason you cannot name. A reproducer that does not
  fail refutes nothing by itself.

Then:

- First: findings (any round, fixed or not) sharing one cause? Don't
  haphazardly patch the unaddressed root cause: `references/one-root-cause.md`.
- Reproduced, and caused by the diff or violating its acceptance contract:
  fix it now. The reproducer stays as a regression test.
- Reproduced but pre-existing and unrelated: record it separately; do not
  grow this branch.
- Refuted: close it and record the reason in the ledger.
- Unconfirmed: never closed, never fixed blind, never dropped. List it for
  the user with what was tried; whether to ship with it open is their call.
- Design finding (`unverifiable: true`): no reproducer applies. Surface it to
  the user as a taste call with file and line. Never auto-fix it, and never
  let it block the ship alone. Classes marked advisory in
  `references/hunt-classes.md` are only listed in the report.

## Rounds

A round is one blind pair over the current integrated diff. After a round's
fixes, run each reproducer, the project gate, the acceptance scenarios and the
deterministic gate again, then run another round. Every round after the
first:

1. Covers the whole diff including the fix commits. A fix is new code and is
   reviewed like the rest.
2. Presents the changed files, and B's `Hunt` lines, in a different order
   from every earlier round.
3. Re-picks B's classes against the diff as it now stands, and adds one
   sibling line per defect the previous round reproduced: "A defect of this
   shape was confirmed at <file:line>: <the shape in one sentence>. Find
   every other site with the same shape." Reviewer A never gets a sibling line.
4. Is deduplicated by you after both reviewers report: a finding that matches
   an earlier adjudicated one by meaning keeps the earlier outcome. Apart
   from the sibling line, no reviewer is shown an earlier finding.

Append each adjudicated round's rows to `<notes>/review-ledger.tsv`
(`references/ledger.md`; a `none` row per silent check). After adjudicating
round 2 onward, before fixes or next reviewers (resumed too):
`node ~/.claude/skills/advice-refine-test-loop/scripts/convergence-check.mts <notes>/review-ledger.tsv <run>…`
(this close-out's). Nonzero → `references/rabbit-hole.md`,
before `one-root-cause.md`; at the cap, report-only.

Stop when two consecutive rounds are empty (nothing new reproduced or
unconfirmed). Cap: four rounds. At the cap without them, report "not
converged" with the count per round; the user decides what happens next.

## Report

Return a compact ledger: rounds run and why the loop stopped, reviewer calls
and model usage when available, B's hunt classes per round, files read,
findings proposed / reproduced / refuted / unconfirmed / fixed / deferred,
design and advisory findings surfaced, commands run, and final gate status.
List every unconfirmed finding with what was tried. Reframes: threads,
verdicts, approach, user asked?

End the report with the `ledger-tally.sh` line for each check that ran in
this close-out. If a line says `drop` or `reword`, or an `exclude?` line
appears that `references/exclusion-decisions.tsv` does not already decide
(by meaning, not wording; see `references/ledger.md`), put it to the user as
one closed question. Change nothing yourself.

Never claim broad audit coverage from this bounded close-out review. Two
empty rounds are two samples, not proof.
