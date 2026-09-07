---
name: orchestrate
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

| Phase | REQUIRED SUB-SKILL / TOOL |
|---|---|
| Design & plan (upstream) | superpowers:brainstorming → superpowers:writing-plans |
| Autonomy loop (stop gate) | ralph-loop plugin (official) — sigil `<promise>…</promise>` + `--max-iterations`; armed in Phase 0 below |
| Cross-context persistence | planning-with-files — `tasks/mission.md`, `tasks/progress.md`, `tasks/findings.md` |
| Per-chunk engine: fresh implementer subagent + two-stage spec→quality review, model tiering, status protocol | superpowers:subagent-driven-development |
| Per-task discipline inside subagents | superpowers:test-driven-development |
| Empirical close-out review (one bounded fresh reviewer, reproduce-before-fix) | advice-refine-test-loop |
| Parallel dispatch mechanics | superpowers:dispatching-parallel-agents |
| Branch completion | superpowers:finishing-a-development-branch |

## Phase 0 — Autonomy contract (REQUIRED, once, before the first subagent)

1. **Stop criteria.** Derive from the plan; ask the user only if underivable:
   - *Sigil statement* — one sentence TRUE only at genuine completion.
     Template: `All acceptance criteria in tasks/mission.md are implemented, the verifier is green, and a fresh-context reviewer confirmed the diff.`
   - *Verifier* — the project's gate command(s): tests + lint + build, exit 0 = green.
   - *Max iterations* — finite, default 25.
2. **Write `tasks/mission.md`** (planning-with-files format): goal,
   acceptance criteria (checkboxes), verifier command, sigil statement,
   cancel instructions (`/cancel-ralph`). Also create an
   empty `tasks/progress.md` now — doctrine and the loop prompt tell every
   iteration to read it, so it must exist from iteration 1. Seed
   `tasks/session-constraints.md` with any durable user directives already
   given this session (see Constraint pinning below); create it empty if
   none.
3. **Arm the loop** — run from the WORKTREE ROOT (the state file records it as
   `armed_in:`, which is how cwd-shaped consumers find the loop).
   Set `SIGIL` to the mission-specific statement from mission.md first —
   NEVER pass a placeholder: an unsubstituted promise can never match, so
   the loop would only end at max-iterations. Sigil rules: single line, no
   double quotes (the setup script YAML-wraps it verbatim), TRUE only at
   genuine completion.

   ```bash
   # Preflight: arm from a repo/worktree root. The gate binds THIS session id,
   # not the directory, but `armed_in:` is recorded from $PWD and the mission
   # files are read relative to it.
   case "$PWD" in
     "$HOME"|"$HOME/.claude"|"$HOME/Repos") echo "REFUSE: arm only from a dedicated worktree root"; exit 1;;
   esac
   [[ -e .git ]] || { echo "REFUSE: not a repo/worktree root"; exit 1; }  # .git is a FILE in linked worktrees, a dir in main checkouts; -e covers both

   mkdir -p tasks && : > tasks/progress.md   # must exist from iteration 1 (doctrine reads it)
   SIGIL='All acceptance criteria in tasks/mission.md are implemented, the verifier is green, and a fresh-context reviewer confirmed the diff.'
   case "$SIGIL" in *'"'*|*'$'*|*'`'*|*'\'*)
     echo 'REFUSE: sigil must not contain " $ ` or \ (setup script YAML-wraps it verbatim; a violation makes the sigil unmatchable and the loop runs to max-iterations)'; exit 1;;
   esac
   # The SESSION-BOUND fork, not the upstream plugin's setup-ralph-loop.sh.
   # Upstream writes a cwd-relative .claude/ralph-loop.local.md with an empty
   # session_id: that gates every session stopping in the directory, gates the
   # arming session nowhere else, and drops a file inside the repo — which a
   # public repo's auto-commit cron then publishes.
   SETUP=~/.claude/scripts/ralph-loop-setup.sh
   [[ -f "$SETUP" ]] || { echo "REFUSE: session-bound ralph setup missing — loop would silently never arm"; exit 1; }
   # `bash "$SETUP"`, never "$SETUP" directly: a #!/bin/bash shebang does not
   # resolve on NixOS (no /bin/bash) — direct exec fails exit 126.
   bash "$SETUP" "Autonomous mission: read tasks/mission.md, tasks/session-constraints.md, and tasks/progress.md, then continue from where progress.md left off (empty progress.md = start at the first unchecked acceptance criterion). Orchestrate via subagents; do not implement inline; paste session-constraints.md verbatim into every subagent briefing." \
     --completion-promise "$SIGIL" --max-iterations 25
   source ~/.claude/scripts/ralph-loop-state.sh
   [[ -f "$(ralph_state_file "$CLAUDE_CODE_SESSION_ID")" ]] || { echo "ARM FAILED: state file missing — stop gate is NOT live"; exit 1; }
   [[ -e .claude/ralph-loop.local.md ]] && { echo "ARM WRONG: upstream wrote state into the repo — the gate is cwd-bound, not session-bound"; exit 1; }
   ```

   The setup script REFUSES to arm without a valid `CLAUDE_CODE_SESSION_ID`,
   rather than falling through to a directory-scoped gate — an unbound loop
   that looks armed is worse than one that refuses. Run ONE orchestrator
   session per worktree anyway (two sessions racing the same mission files is
   its own problem). Subagents are safe: only the `Stop` event is registered,
   which fires when the MAIN agent finishes responding — subagent completion
   is the separate `SubagentStop` event and never triggers the gate (verified
   against official hooks docs 2026-08-02).

## Sigil discipline — verifier earns the sigil, sigil earns the stop

- The ONLY legitimate stop is `<promise><sigil statement></promise>`,
  emitted ONLY when the statement is literally TRUE:
  1. every acceptance criterion in mission.md checked off;
  2. verifier freshly run this iteration, exit 0;
  3. fresh-context reviewer (compact close-out advice-refine-test-loop) confirms
     the combined diff, zero BLOCKER/HIGH.
- Blocked ≥3 iterations on an external dependency (credential, human-only
  step): log it in `pending_for_human.md`, mark the criterion blocked in
  mission.md, let the loop hit max-iterations. NEVER emit a false sigil.
- Deferral vocabulary mid-loop ("Would you like me to…", "should I
  continue?", "next step would be…") is a bug: the loop re-feeds the same
  prompt, so asking costs an iteration and answers nothing. Act instead.
- Emit the sigil ALONE as the final message, and never quote `<promise>`
  tags in prose mid-run: the stop hook extracts the FIRST tag in the last
  text block, so a quoted tag earlier in the message can shadow the real
  sigil (costs an iteration; self-heals next turn, but don't).

## Context discipline (parent = orchestrator, not implementer)

- **Parent holds NO code context.** Never Read source files, never paste
  diffs or test output into orchestrator context. Hold only: task list,
  mission.md, progress.md, dispatch decisions, terse subagent reports.
- **Subagents hold the heavy context and die.** All reads/edits/test runs
  live in subagents. Report cap ~15 lines: status; files touched; gate
  result; decisions; next-step recommendation.
- **Model tiering by task difficulty.** When dispatching a subagent, weigh
  the task's difficulty and pass the Agent tool's `model` param
  accordingly: the top-tier default (Fable-class, scarce monthly quota)
  only for the hardest reasoning — adversarial review, subtle-defect
  hunting, design-critical implementation; `model="claude-opus-5"`
  (separate quota bucket) for routine implementation, mechanical fixes,
  repro/validation runs, test truthing, exploration. Burning the scarce
  bucket on routine work buys no quality and has killed a mid-run agent on
  a spend limit (2026-09-04); when the default bucket 429s mid-mission,
  re-dispatch the dead agent's brief on opus rather than waiting for the
  window.
- **progress.md after every subagent return.** Append what completed, gate
  state, decisions, next. Files — not the transcript — are the resumption
  source.
- **Constraint pinning.** Negative/durable directives are the first thing
  compaction kills: they produce no artifact, no diff, no task — a
  summarizer preserves the task narrative and drops "don't use X" as a
  non-event. So: the moment the user states a directive that outlives the
  current task ("don't X", "always Y", "never Z", "keep it A"), append it
  datestamped to `tasks/session-constraints.md` IN THAT SAME TURN — at
  directive-time the directive is the freshest thing in context, so the
  write itself can't drift. Tasks are not constraints ("fix the typo" is
  a task). Include the file's contents VERBATIM in every subagent
  briefing — constraints must propagate mechanically, not via your
  summary of them.
- **Recitation.** At each ralph iteration start: re-read mission.md,
  progress.md, session-constraints.md, and `git log --oneline` since
  mission start. Trust files and git over transcript memory.
- **Git ratchet.** Commit per green unit in the worktree. An iteration
  that ships nothing commits nothing — but still appends to progress.md.
- **Compact survival.** SessionStart(startup|compact|clear) runs
  `hooks/compact-reinject.sh`; its stdout is injected into model context —
  that hook IS the doctrine-persistence channel while a loop is armed. If
  doctrine feels missing post-compact, re-read this skill.

## What THIS skill adds (the delta)

1. **Research gate.** Before a novel or safety-critical chunk (crypto,
   GDPR, distributed/concurrency, A/B stats): research SOTA first via the
   research-agent. Skip for mechanical repeats — discernment, not ritual.
2. **Conflict-aware sequencing.** Parallelize chunks with **disjoint
   files**; **serialize chunks touching the same hot path/function** —
   two edits to one `upsert` collide, and sequencing beats merge-hell on
   safety-critical paths. Rule of thumb: parallelize READS freely,
   serialize WRITES unless provably disjoint.
3. **Sad-path e2e, proven non-flaky.** Every chunk ships blackbox tests
   for happy AND sad paths (races, reconnects, rollbacks). Run them
   repeatedly; make chaos deterministic (e.g. scope a
   `pg_terminate_backend` kill by `application_name`).
4. **Gate green continuously**, not as a phase — the project's configured
   gate after every edit.
5. **Close-out sweep** (on "ship this" / before merge): invoke
   advice-refine-test-loop once over the finished, integrated diff. It owns
   bounded context selection, risk routing, independent review and empirical
   reproduction. Do not add default security, operations or multi-reviewer
   fan-out beside it; escalate only under that skill's explicit conditions.
6. **Integration ritual.** Fold branches into ONE integration branch →
   run the full gate ONCE on the combined result (catches cross-branch
   semantic conflicts per-branch gates miss) → run the close-out sweep once →
   merge on green → clean up worktrees/branches. Do not review again unless
   the close-out fix added production surface outside its original capsule.
7. **Taste calls surface with artifacts.** Design/UX/naming decisions go
   to the user with screenshots/before-afters BEFORE merge; correctness
   work merges on green without asking.
8. **Fix, don't defer — and defer only for a reproducible hard block.**
   Real HIGHs found mid-flight get a focused follow-up subagent NOW.
   Legitimate deferrals are limited to three empirical categories:
   (a) **compile/gate-blocked** by a bug outside the diff that a small
   fix can't repair inside the current runway; (b) **externally blocked**
   on a credential, human interactive login, or paid service the loop
   can't self-serve; (c) **user vetoed** the scope. Everything else
   ships in this run. Specifically NOT legitimate deferral reasons:
   "time budget", "audit found existing implementation partially" (the
   brief spec is authoritative — half-present means ship the other
   half in the same run), "risky headless" (that's what TDD + subagent
   sandboxes exist for), "out-of-scope for a visual-fix branch" (if the
   brief listed it, it's in scope), "wants its own advisor pass"
   (bundle the advisor pass — see Phase G, not next slice).
9. **Handover message before sigil.** The sigil emission is preceded (in
   the SAME text block, tag last) by a founder-facing handover with three
   fixed sections — reduced or hidden only when a section is empty:
   1. **Executive summary** — numbered list of what was built and why,
      including tooling / meta / skill / rule changes (not just product
      code). One line per item; grouped by mission acceptance criterion
      when possible. Purpose: the founder scans this to know what
      changed without re-reading commits.
   2. **Visual review** — if ANY view file was edited (HTML/templates,
      client TS rendering, HTML-returning handlers), run
      `~/.claude/skills/screenshot-review/serve.sh <shots-dir>` on the
      regenerated shots dir. Include the served URL + kill command in
      the handover. Do not skip this because "founder can start the
      server themselves"; the friction of starting it is what defers the
      review to never. Omit only if the diff is pure backend / infra.
      **Ordering**: fanouts + component playgrounds go FIRST in the
      served list — founder picks + component-level fixes cascade into
      surfaces, so locking them before reviewing surfaces avoids
      re-reviewing surfaces after every component change. Rename files
      with numeric prefixes (`1-fanout-*.png`, `2-component-*.png`,
      `3-surface-*.png`) so the tool's natural sort surfaces the
      cascade order.
   3. **Waiting PRs** — if branch is unmerged, list clickable PR URLs
      (or, if the halt discipline forbids `git push`, the exact push +
      `gh pr create` commands the founder can paste). One-liner per PR
      with the branch name, base, commit count.
   Then, and only then, emit the sigil ALONE at the end of the message.
   The stop hook reads the FIRST `<promise>` tag; a handover-then-sigil
   ordering is safe because the handover has no `<promise>` tags.

## Red flags

- Implementing inline "because it's faster" — context bloat is how
  quality dies at hour six
- Two subagents editing the same hot function in parallel
- Merging chunk branches straight to main without the combined gate
- Accepting an advisor BLOCKER without reproducing it (advisors
  hallucinate; the 2026-07-07 egress BLOCKER was refuted empirically)
- Happy-path-only e2e
- Emitting the sigil without a fresh verifier run + reviewer confirmation
- Parent Reading source files "just to check" — dispatch a subagent
- Asking the user mid-loop anything mission.md already answers
- Briefing a subagent with a paraphrase of user constraints instead of
  pasting `tasks/session-constraints.md` verbatim — paraphrase is how
  constraints silently die in handoff
- Writing "Emergency exits" catch-alls in a subagent brief ("if X blows
  the budget, ship minimum + defer rest"). Emergency-exit sections are
  loopholes subagents lean on to escape rule 8. Each legitimate exit
  must name a REPRODUCIBLE hard block (compile error, external auth
  wall) — not a scope/time/audit judgment
- Sigil declared without the fresh-context `advice-refine-test-loop` run —
  the sigil requires reviewer confirmation
  of the combined diff; skipping reviewer = false sigil
- Subagent report frames "AUDIT" as delivery when the brief said
  "PORT" / "IMPLEMENT" / "SHIP". Audit-only completes only when the
  brief explicitly said "audit only". Half-present in existing code
  means SHIP THE OTHER HALF in the same run
- Layout/UX enforcement landed at SOFT-WARN when the brief said
  HARD-FAIL, with a note "will flip after baseline". The baseline is a
  fixture you write in the same commit; the flip is one line. Don't
  ship the safety-net-off version

## Scope guard

Default for anything you'd branch for. NOT for trivial edits or quick
questions — subagents and branches are pure overhead there. Existing
"pick the simpler option / don't scope-creep" rules still govern.
