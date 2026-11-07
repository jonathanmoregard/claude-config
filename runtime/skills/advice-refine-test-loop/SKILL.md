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
- repository access for evidence-driven exploration.

The reviewer chooses which files, callers, tests, and configuration to inspect.
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
   them even when it quotes nothing. Two fresh, read-only reviewers against
   the settled tree; neither sees the other's prompt, findings, or existence.
   Sending both in ONE tool-use block is the cleanest guarantee of that.
3. Merge by meaning. Convergence is not confidence — shared blind spots are
   real, so a finding both report still reproduces before any edit.

Two reviewers is the whole budget.

Reviewer A, falsification:

```text
Assume this finished change can ship broken. Try to falsify its acceptance
claims and important safety properties. Use your own discernment to decide what
to inspect, beginning with the diff. Report only material defects. Each must
name a file and line, state one falsifiable claim, and give a concrete test,
request, or app interaction that would demonstrate it. Do not suggest cleanup
or edits. Return no finding that lacks an observable failure. Record files read.
```

Reviewer B, targeted hunt: its prompt sits in that same file, above the
classes. Send only the classes whose trigger this diff satisfies — never the
whole list — and keep B's two finding buckets, defect and design, apart as the
file defines them.

Use another vendor or specialist only when the user requests a deep audit, a
review exposes unresolved contradictory evidence, or failure could cause
irreversible security, privacy, or data loss.

## Reproduce, Then Act

Independently reproduce every defect finding before editing. Test the exact
claimed path and record expected versus actual behavior. Batch findings through
one verifier context; do not create one agent or worktree per finding unless
concurrent reproducers would mutate shared state.

- Unreproduced: close it without editing — meaning the claimed path was
  exercised and the claim failed, never that the reproducer was awkward.
- Design finding (`unverifiable: true`): no reproducer applies. Surface it to
  the user as a taste call with file and line. Never auto-fix it, and never let
  it block the ship alone.
- Caused by the diff or violating its acceptance contract: fix it now.
- Confirmed but pre-existing and unrelated: record it separately; do not grow
  this branch or restart review rounds.

After fixes, run each reproducer, the project gate, acceptance scenarios, and
the deterministic gate again. Re-review only when the fix added production
surface outside the original capsule. At most one such follow-up pass.

## Report

Return a compact ledger: reviewer calls and model usage when available, B's
hunt classes, files read, findings proposed/reproduced/fixed/deferred, design
findings surfaced, commands run, and final gate status. Never claim broad audit
coverage from this bounded close-out review.
