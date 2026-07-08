---
name: autonomous-orchestration
description: Use when starting substantial multi-part work — anything you'd branch for. Features, migrations, redesigns, autonomous "build build build" sessions, large multi-chunk reviews. Not for trivial single-file edits or quick questions.
---

# Autonomous Orchestration

Orchestrate, don't implement inline. Heavy work and long tool output live
in fresh-context subagents with self-contained briefs; only concise
reports return. This keeps quality high AND the orchestrator's context
lean. Provenance: the 2026-07-06/07 Kablong autonomous session shipped
crypto-shredding, a LISTEN/NOTIFY bus with chaos tests, a race fix, perf
work, and a UI redesign with zero regressions reaching main using exactly
this procedure.

## Composition — don't reimplement these

| Phase | REQUIRED SUB-SKILL |
|---|---|
| Design & plan (upstream) | superpowers:brainstorming → superpowers:writing-plans |
| Per-chunk engine: fresh implementer subagent + two-stage spec→quality review, model tiering, status protocol | superpowers:subagent-driven-development |
| Per-task discipline inside subagents | superpowers:test-driven-development |
| Adversarial review rounds (tier-above advisor, reproduce-before-fix, loop to zero BLOCKER/HIGH) | advice-refine-test-loop |
| Parallel dispatch mechanics | superpowers:dispatching-parallel-agents |
| Branch completion | superpowers:finishing-a-development-branch |

## What THIS skill adds (the delta)

1. **Research gate.** Before a novel or safety-critical chunk (crypto,
   GDPR, distributed/concurrency, A/B stats): research SOTA first via the
   research-agent. Skip for mechanical repeats — discernment, not ritual.
2. **Conflict-aware sequencing.** Parallelize chunks with **disjoint
   files**; **serialize chunks touching the same hot path/function** —
   two edits to one `upsert` collide, and sequencing beats merge-hell on
   safety-critical paths.
3. **Sad-path e2e, proven non-flaky.** Every chunk ships blackbox tests
   for happy AND sad paths (races, reconnects, rollbacks). Run them
   repeatedly; make chaos deterministic (e.g. scope a
   `pg_terminate_backend` kill by `application_name`).
4. **Gate green continuously**, not as a phase — the project's configured
   gate after every edit.
5. **Close-out sweep** (on "ship this" / before merge), beyond what
   per-chunk review sees:
   - advice-refine-test-loop over the finished diff;
   - security scan + self-red-team;
   - **operational-readiness lens** (the class diff-review is blind to):
     unbounded maps/rows, rate-limiting on TCP peer vs XFF, backups over
     a live WAL DB, missing PRAGMAs/session settings, the N>1 scale-out
     ledger;
   - **multi-lens parallel reviewers** (race/concurrency, security+crypto,
     domain completeness, perf+scaling, coverage+leaks+ops). Findings
     multiple reviewers **converge** on are high-confidence; reproduce
     every BLOCKER/HIGH before acting.
6. **Integration ritual.** Fold branches into ONE integration branch →
   run the full gate ONCE on the combined result (catches cross-branch
   semantic conflicts per-branch gates miss) → consolidated adversarial
   re-review seeded with your specific worries → merge on green → clean
   up worktrees/branches.
7. **Taste calls surface with artifacts.** Design/UX/naming decisions go
   to the user with screenshots/before-afters BEFORE merge; correctness
   work merges on green without asking.
8. **Fix, don't defer.** Real HIGHs found mid-flight get a focused
   follow-up subagent now — unless large/risky/out-of-scope, then state
   the deferral cost explicitly.

## Red flags

- Implementing inline "because it's faster" — context bloat is how
  quality dies at hour six
- Two subagents editing the same hot function in parallel
- Merging chunk branches straight to main without the combined gate
- Accepting an advisor BLOCKER without reproducing it (advisors
  hallucinate; the 2026-07-07 egress BLOCKER was refuted empirically)
- Happy-path-only e2e

## Scope guard

Default for anything you'd branch for. NOT for trivial edits or quick
questions — subagents and branches are pure overhead there. Existing
"pick the simpler option / don't scope-creep" rules still govern.
