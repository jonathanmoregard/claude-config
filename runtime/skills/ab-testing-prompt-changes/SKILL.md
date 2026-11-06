---
name: ab-testing-prompt-changes
description: Use when a proposed edit to a skill, rule, system prompt, CLAUDE.md or agent definition is being settled by argument, intuition or a single anecdote — someone asks "does this change actually help?", "prove this prompt edit works", "test whether the agent behaves better with this", or wants to compare two prompt/skill variants empirically. Also triggers on: "empirically validate" a proposed improvement, "reconstruct the situation it's intended to improve", validating auto-generated proposals before applying them.
---

# A/B-testing prompt changes

A change to a skill, rule or prompt is a hypothesis about agent behaviour.
Settle it by replaying a **real past failure whose right answer is known**,
under two variants differing by one line. Arguing produces no evidence.

## Gate: three prerequisites

Without all three, stop — you are testing vibes: (1) a real past failure;
(2) ground truth discovered **later**, by other means; (3) a commit that
**predates** the discovery. Never manufacture the failure — reintroducing a bug
you already know, or inventing a synthetic sibling because the real case looks
weak, destroys the only thing that made it evidence.

## What unaided designs reliably skip

Observed in 3/3 baseline designs. Each is mandatory.

**Contamination proof — and report it.** Pin a worktree to the commit before the
fix, then prove the answer isn't in-tree: grep everything (`.py`, `.md`, tests,
comments, plan docs) for the answer and its paraphrases, and prove later
artifacts are descendants (`git merge-base --is-ancestor <later> <pin>` must
FAIL). Print it as a pass/fail table. Denying Bash does not hide git history;
`worktrees` verifies the pin, then **seals** each tree by deleting `.git`
(`ab_harness.py --help` says why that is necessary, and what the greps mean).

**Pre-register to a FILE, before any run.** Prose saying "we'll decide in
advance" is not pre-registration. Binary primary outcome with explicit
non-qualifiers, secondaries, analysis plan, decision rule. The harness refuses
to run if the rubric, prompt or either arm changed after the lock.

**Inconclusive policy.** A run that exits non-zero, is truncated by the cap, or
returns no final message is INCONCLUSIVE: excluded from the denominator,
reported separately, **never retried into a replacement**. Fixed n, one look.

**Measure the budget cap; never double a single probe.** Probe ≥2 times, cap at
3–4x the *worst* probe, never below the priciest run this rig has produced,
floor ~$1. Probes are short; the runs you must not truncate are the long
exploratory ones. A $0.256 probe implied $0.51 — that rig had already produced
**$1.29** runs; elsewhere a guessed $0.75 cap would have voided 9 of 12 runs.

**The cap is a truncation guard, not a bill — do not stop and ask to spend
it.** Those `$` are cost-*equivalent* accounting the CLI computes from token
usage, and they print identically whether the run is metered or not. The
harness shells out to `claude -p`, which inherits the caller's auth: with no
`ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` and a `claudeAiOauth` entry in
`~/.claude/.credentials.json`, runs draw on the **subscription quota**, exactly
like the session dispatching them. There is no incremental charge to seek
approval for, and the real constraint is rate-limit quota in the rolling
window, not dollars. Check the auth mode rather than inferring from the dollar
signs; if a `CLAUDE.md` gates on "billed beyond the subscription", that gate
does not fire here. **A designed, contamination-clean, pre-registered
experiment that never runs produces exactly as much evidence as arguing did.**
Keep every cap-setting rule above — they stop runaway runs and truncated arms,
which is a data-quality control, not a spending control.

**Disclose designer bias.** If the same person proposed the change, wrote both
arms, the task prompt and the rubric, say so.

## Also

- **One variable.** Arm B = Arm A + one surgical change; copy Arm A verbatim and
  md5-check it. **Never edit the installed skill** — write variants to files and
  inject with `--append-system-prompt` plus `--setting-sources ""`, which also
  stops the installed skill loading itself into Arm B.
- **Blind the grader**: output and criteria only, shuffled and re-keyed — never
  the hypothesis, arm labels or expected direction. Cross-check ≥2 transcripts
  yourself before seeing any grade.
- **Scrub the task prompt of hints** — the reference prompt named the file
  containing the answer, inflating both arms.
- **`run` takes ~8 min for 20 runs** at concurrency 4 and outlives your shell
  wrapper; `nohup` dies with the session, so launch it detached with
  `systemd-run --user` (see `detached-long-commands`). It resumes: finished runs
  are skipped, so a killed batch never silently re-spends.
- **Watch secondaries for opposite-direction effects**: one reference primary
  went 3/6 → 4/6 (null) while a declared secondary went 4/6 → **1/6**.
- **Power.** n=6/arm detects only huge effects; ~25–36/arm for a 30–40pp swing.
  State the smallest detectable effect. **A null is a valid result** — report it
  plainly, no "trending toward".

## Red flags — stop

- No grep of the pinned tree for the answer
- Rubric edited after seeing a run
- A run retried because it failed "for a boring reason"
- n raised after a peek
- Budget cap from intuition, or from one short probe
- Overwriting `~/.claude/skills/<x>/SKILL.md` to swap arms
- No dollar figure anywhere in the design
- Pausing a locked, contamination-clean design to ask permission to spend —
  check the auth mode first; on a subscription it is quota, not a charge

## Files

`ab_harness.py` (`contamination | worktrees | probe | run | grade`, one JSON
config — read `--help`), `config.example.json`,
`PREREGISTRATION-template.md`, `RESULTS-template.md`. The harness's own suite
is `tests/ab-testing-prompt-changes/` in the claude-config checkout, outside
what this skill ships. Two real worked examples,
both null: `~/Repos/skill-ab/RESULTS.md` and, there,
`experiments/recurrence-escalation/RESULTS.md`.
