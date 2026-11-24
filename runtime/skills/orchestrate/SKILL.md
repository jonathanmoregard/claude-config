---
name: orchestrate
description: Use for substantial multi-part implementation, migrations, redesigns, or unattended autonomous work. Not for questions, research-only work, or a trivial local edit.
---

# Autonomous Orchestration

The mission files carry state across turns and compactions. `finishing-up`
defines delivery done. Orchestrate sequences the work; it does not duplicate
either.

## Start Once

1. Write `tasks/mission.md` with the goal, checkbox acceptance criteria, and the
   project's verifier command. The mission is done only after integrated
   verification, close-out review, and `finishing-up`'s delivery gate.
2. Create `tasks/progress.md`. Read `tasks/session-constraints.md`, creating it
   if absent. Durable user directives go there immediately and are pasted
   verbatim into every subagent brief.

One orchestrator owns one worktree.

## Continue The Mission

At every turn, read mission, progress, constraints, and current git state.
Continue from the first unchecked acceptance criterion: choose the next action
that most directly closes it. Do not stop with a plan, status report, or offer
to continue while executable work remains.

When the only remaining work is a background subagent or job you launched,
end the turn. Its completion notification re-invokes you; then read its
result, update `tasks/progress.md`, and continue. Do not poll, sleep, or
narrate while waiting.

Use judgment about delegation and context depth:

- Group work by coherent behavior, not arbitrary file or task counts.
- Dispatch a fresh subagent when implementation is context-heavy, independently
  parallelizable, or benefits from isolation. The parent may inspect enough
  code and evidence to route work and integrate results.
- Parallelize reads freely; parallelize writes only when ownership is disjoint.
- Brief the subagent with its acceptance criterion, worktree, constraints,
  relevant evidence, and required checks. Do not mandate separate spec and
  quality reviewers for every increment.
- Keep verbose tool output in the working context that needs it. Return a terse
  result: behavior changed, files touched, checks run, decisions, and blocker.

After an edit, run the fastest relevant targeted check before building on it.
At a completed criterion or context boundary, update `tasks/progress.md` with
evidence and the next unchecked criterion. Commit coherent green units.

Exercise the real behavior named by the specification. Drive the app or run a
black-box scenario when that is the acceptance surface. If rendered UI changed,
invoke `visual-verify`. Add sad-path coverage where the behavior or risk calls
for it; no test category runs merely because every chunk must look identical.

If an approach fails repeatedly, diagnose or consult the advisor before trying
variants. A credential, human-only action, or external service wait is a block;
difficulty or remaining implementation is not.

## Close The Loop

Once all implementation criteria are checked:

1. Inspect the integrated diff and run the full gate once from a settled tree.
2. Run acceptance scenarios and app/visual verification not already covered by
   that gate.
3. Invoke `advice-refine-test-loop` once over the finished, integrated diff.
   Reproduce material findings, fix confirmed regressions, and rerun affected
   evidence plus the full gate.
4. Invoke `finishing-up`. Run its fresh `git`/`gh` readiness checks and act on
   the measured blockers. That skill owns commit, push, PR, CI, mergeability,
   follow-up scheduling, and handover; do not restate its ladder here.
5. Report the mission done only after criteria, verification, review, and
   delivery state make that literally true.

When `finishing-up` proves that no productive action remains because CI,
credentials, or a human action must happen later, create its durable follow-up
first, then report the measured blocker. Never burn turns waiting, and never
stop merely because work is difficult.
