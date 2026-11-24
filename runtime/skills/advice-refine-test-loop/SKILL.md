---
name: advice-refine-test-loop
description: Use when a finished, integrated non-trivial diff needs independent close-out review before merge or release, especially after "ship this", "look through carefully", or an unattended build. Not for per-edit, per-task, or per-chunk checks.
---

# Empirical Close-out Review

Fresh blind reviewers propose falsifiable defects; observed behavior decides
what gets fixed; the diff's risk tier sets the effort. Do not run per edit,
task, or chunk; the project tests serve there.

## Entry Gate

1. Wait until the integrated tree is settled and no agent is editing it.
2. Run the project's tests, lint/build checks, and acceptance scenarios.
3. Run `~/.claude/skills/advice-refine-test-loop/scripts/deterministic-gate.sh --json`
   once. Exit `1` means fix its findings; any other nonzero exit means
   report that the gate was unavailable.
4. If rendered UI changed, invoke `visual-verify` first and include its evidence
   in the review capsule.
5. Note the start time, then classify:
   `python3 ~/.claude/hooks/artl_gate.py tier` (`--base <ref>` for a stacked
   branch). It prints the tier and why. You may raise a tier when the diff is
   riskier than its paths show, never lower one.

| Tier | Review |
|---|---|
| T0 | None. The gates above are the review; report the tier line. |
| T1 | Round 1 is reviewer A alone, B's triggered `Hunt` lines appended; then delta rounds. |
| T2 | The blind pair, then delta rounds (see Rounds). |
| T3 | T2, plus A's prompt once through `scripts/codex-advise.sh` in round 1; on a nonzero exit, report that pass unavailable. |

Do not invite an LLM reviewer while a deterministic gate is red.

## Build A Bounded Capsule

Give the reviewer only:

- the goal and acceptance criteria;
- the integrated diff and diff stat;
- commands already run and their outcomes;
- repository access for evidence-driven exploration;
- the exclusion list, when it has entries
  (`references/exclusions.md`), under the line "Do not report:".

The reviewer chooses which files to inspect. Inspection depth is the
reviewer's judgment. Never restrict inspection to changed hunks: a historical
miss hid in an unchanged invariant elsewhere in a changed file. Record files
read.

## Round 1: Review Twice, Blind

1. Pick reviewer B's hunt classes FIRST, before either dispatch, from
   `~/.claude/skills/advice-refine-test-loop/references/hunt-classes.md`.
2. Write both prompts before dispatching either, and never revise B's after A
   reports: a prompt composed once A's findings are in context is anchored on
   them even when it quotes nothing. Keep both in flight at once: one
   tool-use block, a later message only where the dispatch returns before the
   reviewer reports. Two fresh, read-only reviewers; neither sees the other's
   prompt, findings, or existence.
3. Merge by meaning. Convergence is not confidence — shared blind spots are
   real, so a finding both report still reproduces before any edit.

Only round 1 is a pair.

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
lacks an observable failure. Report at most five, most severe first; if none
qualifies, say `No findings.` Record files read.
```

Reviewer B, targeted hunt: its prompt sits in that same file, above the
classes. Send only the classes whose trigger this diff satisfies — never the
whole list — and keep B's two finding buckets, defect and design, apart as the
file defines them.

Beyond T3, another vendor or specialist only on the user's deep-audit request
or unresolved contradictory evidence.

## Adjudicate, Then Act

Every defect finding gets exactly one of three outcomes before any edit. Test
the exact claimed path; record expected versus actual. Batch findings through
one fresh verifier context, never the one that wrote the code.

- **Reproduced** — a test, request or app interaction shows the claimed
  failure.
- **Refuted** — the claimed path was exercised, the failure did not occur,
  and you can name the code (file and line) that makes the claim false. A
  comment is not that code.
- **Unconfirmed** — everything else: the reproducer was awkward, could not be
  built, or passed for a reason you cannot name. A reproducer that does not
  fail refutes nothing by itself.

Then:

- First, append this round's rows to `<notes>/review-ledger.tsv`
  (`references/ledger.md`; a `none` row per silent check).
- Findings (any round, fixed or not) sharing one cause? Don't haphazardly
  patch the unaddressed root cause: `references/one-root-cause.md`.
- Reproduced, and caused by the diff or violating its acceptance contract:
  fix it now, as a class: the fixer searches the repository for every other
  site with the same shape and fixes them in the same change (each extra
  site gets a `variant` row). The reproducer stays as a regression test.
- Reproduced, pre-existing and unrelated: record it; don't grow the branch.
- Refuted: close it and record the reason in the ledger.
- Unconfirmed: never closed, never fixed blind, never dropped. List it for
  the user with what was tried; whether to ship with it open is their call.
- Design finding (`unverifiable: true`): no reproducer applies. Surface it to
  the user as a taste call with file and line. Never auto-fix it, and never
  let it block the ship alone. Classes marked advisory in
  `references/hunt-classes.md` are only listed in the report.

## Rounds

After a round's fixes, run each
reproducer, the project gate, the acceptance scenarios and the deterministic
gate again. If anything new reproduced or stayed unconfirmed, run the next:

1. One fresh reviewer with A's prompt, over the delta only: the commits since
   the tree the previous round reviewed. A fix is new code; the reviewer may
   read outside the delta to judge it.
2. Its capsule adds each Unconfirmed finding as a claim to settle, and every
   adjudicated finding's claim under "Do not report:". Deduplicate what it
   returns by meaning; a match keeps the earlier outcome.

Stop at the first round with nothing new reproduced or unconfirmed. Cap:
three rounds. A round-3 finding is not fixed in the loop: the fixes are not
converging. Group the reproduced findings by area, name the area that kept
producing them, research its standard design (CLAUDE.md, Build + review), and
ask one closed question: redesign <area> (recommended), ship with the findings
open, or one more round. Unattended: write it to `<notes>/pending_for_human.md`
and stop with "not converged; redesign proposed".

## Report

Report the tier, rounds run and why the loop stopped, B's hunt classes,
findings proposed / reproduced / refuted / unconfirmed / fixed / deferred,
design and advisory findings, and final gate status. List every unconfirmed
finding with what was tried. Append the close-out's row to
`<notes>/review-runs.tsv` (`references/ledger.md`, Runs).

End the report with the `ledger-tally.sh` line for each check that ran in
this close-out. If a line says `drop` or `reword`, or an `exclude?` line
appears that `references/exclusion-decisions.tsv` does not already decide
(by meaning, not wording; see `references/ledger.md`), put it to the user as
one closed question. Change nothing yourself.

Never claim broad audit coverage from this bounded close-out review. An
empty round is one sample, not proof.
