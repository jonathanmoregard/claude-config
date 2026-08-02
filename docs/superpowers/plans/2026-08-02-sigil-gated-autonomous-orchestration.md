# Sigil-Gated Autonomous Orchestration (Path B) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (default) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make autonomous runs stop-proof (FM1) and compact-proof (FM2) by arming the official ralph-loop plugin from a new Phase 0 in the autonomous-orchestration skill, gating the completion sigil behind verification, and re-injecting the autonomy contract after compact/clear.

**Architecture:** Three pieces. (1) `~/.claude/skills/autonomous-orchestration/SKILL.md` grows a Phase 0 "autonomy contract" (stop criteria + dangerous-ops manifest + `tasks/mission.md` + arm ralph loop), a sigil-discipline section, and a context-discipline section (parent holds no code context; progress.md ratchet). (2) A new SessionStart hook script `compact-reinject.sh` re-injects the doctrine whenever a ralph state file is armed in the CWD. (3) `settings.json` wires that hook with matcher `startup|compact|clear`. The official ralph-loop plugin (already enabled, Stop hook auto-registered via its `hooks/hooks.json`) provides the stop gate itself — no custom Stop hook is built.

**Tech Stack:** bash, Claude Code hooks (SessionStart matcher API), official `ralph-loop@claude-plugins-official` plugin (state file `.claude/ralph-loop.local.md`, sigil `<promise>TEXT</promise>`), planning-with-files conventions (`tasks/mission.md`, `tasks/progress.md`).

**Context / constraints discovered during design:**

- Repo is `~/.claude` itself (remote `github.com/jonathanmoregard/.claude`). It IS the live config — no worktree; git revert is the rollback. Tree is dirty with unrelated changes → every commit uses targeted `git add <paths>`, never `git add -A`.
- `Edit(~/.claude/hooks/**)` is deny-listed in settings; new hook script therefore lives under `~/.claude/skills/autonomous-orchestration/hooks/` (allow-listed).
- Official stop hook parses `.claude/ralph-loop.local.md` in the session CWD. When the orchestrator arms the loop via safe-bash, `CLAUDE_CODE_SESSION_ID` is not set in the MCP server env → `session_id:` empty → hook's legacy fallthrough blocks stops for ANY session in that directory. Mitigation: autonomous runs happen in dedicated worktrees, so the blast radius is the worktree. Documented in the skill.
- Existing continue-nudge pipeline (`~/.claude/continue-nudge-*.py`) is NOT wired into any hook. Path B parks it — no code change needed, it simply stays unwired.
- Multiple Stop hooks coexist fine: `step-back-detector.py` runs detached/non-blocking; ralph's hook is the only blocker.

---

### Task 1: `compact-reinject.sh` (TDD)

**Files:**
- Create: `/home/jonathan/.claude/skills/autonomous-orchestration/hooks/compact-reinject.sh`
- Test: `/home/jonathan/.claude/skills/autonomous-orchestration/hooks/test-compact-reinject.sh`

- [ ] **Step 1: Write the failing test**

```bash
#!/usr/bin/env bash
# test-compact-reinject.sh — asserts doctrine re-injection behavior.
set -euo pipefail
SCRIPT="$(cd "$(dirname "$0")" && pwd)/compact-reinject.sh"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
cd "$TMP"

# Case 1: no state file → empty output, exit 0
OUT=$(bash "$SCRIPT")
[[ -z "$OUT" ]] || { echo "FAIL: expected empty output without state file"; exit 1; }

# Case 2: armed state file (fresh) → doctrine with iteration + mission pointer,
# NO literal promise text (doctrine must never print the sigil — parroting footgun,
# and the plugin's own systemMessage already shows it), NO staleness warning.
mkdir -p .claude
NOW_TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
cat > .claude/ralph-loop.local.md <<EOF
---
active: true
iteration: 7
session_id:
max_iterations: 25
completion_promise: "All mission.md criteria verified green"
started_at: "$NOW_TS"
---
Autonomous mission: read tasks/mission.md and continue.
EOF
OUT=$(bash "$SCRIPT")
echo "$OUT" | grep -q 'iteration 7'                                || { echo "FAIL: missing iteration"; exit 1; }
echo "$OUT" | grep -q 'tasks/mission.md'                           || { echo "FAIL: missing mission.md pointer"; exit 1; }
echo "$OUT" | grep -qi 'never end a turn'                          || { echo "FAIL: missing bias-to-action doctrine"; exit 1; }
echo "$OUT" | grep -qF 'All mission.md criteria verified green'    && { echo "FAIL: doctrine must not print literal sigil"; exit 1; }
echo "$OUT" | grep -q 'WARNING'                                    && { echo "FAIL: fresh loop must not warn stale"; exit 1; }

# Case 3: promise unset (null) → same static doctrine, no <promise> tag rendered
cat > .claude/ralph-loop.local.md <<'EOF'
---
active: true
iteration: 2
session_id:
max_iterations: 0
completion_promise: null
started_at: "2026-08-02T00:00:00Z"
---
body
EOF
OUT=$(bash "$SCRIPT")
echo "$OUT" | grep -q 'iteration 2'    || { echo "FAIL: missing iteration (null promise case)"; exit 1; }
echo "$OUT" | grep -qF '<promise>'     && { echo "FAIL: null-promise doctrine must not render a <promise> tag"; exit 1; }

# Case 4: stale loop (started_at 2020) → staleness WARNING with cancel instructions
cat > .claude/ralph-loop.local.md <<'EOF'
---
active: true
iteration: 3
session_id:
max_iterations: 25
completion_promise: "x"
started_at: "2020-01-01T00:00:00Z"
---
body
EOF
OUT=$(bash "$SCRIPT")
echo "$OUT" | grep -q 'WARNING'                       || { echo "FAIL: stale loop must warn"; exit 1; }
echo "$OUT" | grep -qF 'rm .claude/ralph-loop.local.md' || { echo "FAIL: stale warning must include cancel command"; exit 1; }

# Case 5: corrupted iteration field with command substitution → sanitized to '?', nothing executed
CANARY="$TMP/canary"
cat > .claude/ralph-loop.local.md <<EOF
---
active: true
iteration: \$(touch $CANARY)
session_id:
max_iterations: 25
completion_promise: "x"
started_at: "2020-01-01T00:00:00Z"
---
body
EOF
OUT=$(bash "$SCRIPT")
echo "$OUT" | grep -q 'iteration ?' || { echo "FAIL: corrupted iteration must render as ?"; exit 1; }
[[ ! -e "$CANARY" ]]                || { echo "FAIL: state-file content was EXECUTED"; exit 1; }

echo "PASS"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bash /home/jonathan/.claude/skills/autonomous-orchestration/hooks/test-compact-reinject.sh`
Expected: FAIL — `compact-reinject.sh: No such file or directory` (non-zero exit).

- [ ] **Step 3: Write the implementation**

```bash
#!/usr/bin/env bash
# SessionStart(startup|compact|clear) hook: when a ralph loop is armed in this
# project dir, re-inject the autonomy contract — doctrine decays after compaction.
# Design constraints (advisor-reviewed):
# - NEVER print the literal sigil (parroting footgun; plugin systemMessage
#   already shows it each iteration; also avoids the null-promise nonsense case).
# - State-file values are DATA: iteration is regex-validated before heredoc
#   interpolation so a corrupted file cannot execute via $(…) expansion.
set -euo pipefail

STATE=".claude/ralph-loop.local.md"
[[ -f "$STATE" ]] || exit 0

ITER=$(awk '/^iteration:/{sub(/^iteration: */,""); print; exit}' "$STATE")
[[ "$ITER" =~ ^[0-9]+$ ]] || ITER="?"

AGE_NOTE=""
STARTED=$(awk '/^started_at:/{sub(/^started_at: */,""); gsub(/"/,""); print; exit}' "$STATE")
START_EPOCH=$(date -d "${STARTED:-invalid}" +%s 2>/dev/null || echo 0)
if [[ "$START_EPOCH" -gt 0 ]] && (( $(date +%s) - START_EPOCH > 172800 )); then
  AGE_NOTE=$'\n- WARNING: this loop was armed over 48h ago. If the mission is dead, cancel it: rm .claude/ralph-loop.local.md (or /cancel-ralph). A stale armed loop blocks EVERY session that stops in this directory.'
fi

cat <<EOF
[autonomous-orchestration] Ralph loop ACTIVE (iteration ${ITER}). Context was just compacted, cleared, or restarted — re-arm the doctrine:
- Re-read tasks/mission.md and tasks/progress.md NOW. Trust files and git log over any memory of this conversation.
- You are the orchestrator: dispatch subagents for all reads/edits/tests; hold no code context yourself.
- Bias to action. Never end a turn with only a plan, a summary, or a question that mission.md or the dangerous-ops manifest already answers.
- Stop ONLY via the completion sigil recorded in tasks/mission.md, emitted ONLY when literally TRUE: verifier freshly green AND fresh-context reviewer confirmed. Never emit a false sigil. If no sigil is configured, the loop ends only at max-iterations.${AGE_NOTE}
EOF
```

- [ ] **Step 4: Run test to verify it passes**

Run: `bash /home/jonathan/.claude/skills/autonomous-orchestration/hooks/test-compact-reinject.sh`
Expected: `PASS`, exit 0. Also run `bash -n` on both files (syntax gate).

- [ ] **Step 5: Commit**

```bash
cd /home/jonathan/.claude
git add skills/autonomous-orchestration/hooks/compact-reinject.sh skills/autonomous-orchestration/hooks/test-compact-reinject.sh
git commit -m "feat(autonomous-orchestration): compact-reinject hook re-arms doctrine when ralph loop active"
```

---

### Task 2: Wire hook in settings.json

**Files:**
- Modify: `/home/jonathan/.claude/settings.json` (SessionStart array, after the `claude-kitty-pane-record` entry ~line 165-172)

- [ ] **Step 1: Add the hook entry**

Insert into the `SessionStart` array:

```json
{
  "matcher": "startup|compact|clear",
  "hooks": [
    {
      "type": "command",
      "command": "[ \"${CLAUDE_CODE_ENTRYPOINT:-cli}\" = \"cli\" ] || exit 0; bash $HOME/.claude/skills/autonomous-orchestration/hooks/compact-reinject.sh"
    }
  ]
}
```

`startup` is included deliberately: a crashed session leaves the state file armed; a fresh session in that worktree should resume the mission, not wander.

Note: the hook's **stdout IS the doctrine-injection channel** — SessionStart hook stdout is added to model context. Disabling this wiring silently loses the entire compact-survival guarantee; it is not cosmetic.

- [ ] **Step 2: Validate JSON**

Run: `jq empty /home/jonathan/.claude/settings.json && echo OK`
Expected: `OK`

- [ ] **Step 3: Manual smoke of the exact command line**

```bash
cd $(mktemp -d) && mkdir -p .claude && printf -- '---\nactive: true\niteration: 3\nsession_id:\nmax_iterations: 25\ncompletion_promise: "smoke"\nstarted_at: "2026-08-02T00:00:00Z"\n---\nbody\n' > .claude/ralph-loop.local.md
sh -c '[ "${CLAUDE_CODE_ENTRYPOINT:-cli}" = "cli" ] || exit 0; bash $HOME/.claude/skills/autonomous-orchestration/hooks/compact-reinject.sh'
```

Expected: doctrine block printed, mentions `iteration 3`, references "the completion sigil recorded in tasks/mission.md", and does NOT contain `<promise>smoke</promise>` — the script never prints the literal sigil (that's the round-1 parroting fix; do not "repair" the script to print it).

- [ ] **Step 4: Commit**

```bash
cd /home/jonathan/.claude
git add settings.json
git commit -m "feat(hooks): SessionStart compact-reinject wiring (startup|compact|clear)"
```

---

### Task 3: Retrofit autonomous-orchestration SKILL.md

**Files:**
- Modify: `/home/jonathan/.claude/skills/autonomous-orchestration/SKILL.md` (full-file replacement below)

- [ ] **Step 1: Replace SKILL.md with the following content**

````markdown
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

| Phase | REQUIRED SUB-SKILL / TOOL |
|---|---|
| Design & plan (upstream) | superpowers:brainstorming → superpowers:writing-plans |
| Autonomy loop (stop gate) | ralph-loop plugin (official) — sigil `<promise>…</promise>` + `--max-iterations`; armed in Phase 0 below |
| Cross-context persistence | planning-with-files — `tasks/mission.md`, `tasks/progress.md`, `tasks/findings.md` |
| Per-chunk engine: fresh implementer subagent + two-stage spec→quality review, model tiering, status protocol | superpowers:subagent-driven-development |
| Per-task discipline inside subagents | superpowers:test-driven-development |
| Adversarial review rounds (tier-above advisor, reproduce-before-fix, loop to zero BLOCKER/HIGH) | advice-refine-test-loop |
| Parallel dispatch mechanics | superpowers:dispatching-parallel-agents |
| Branch completion | superpowers:finishing-a-development-branch |

## Phase 0 — Autonomy contract (REQUIRED, once, before the first subagent)

1. **Stop criteria.** Derive from the plan; ask the user only if underivable:
   - *Sigil statement* — one sentence TRUE only at genuine completion.
     Template: `All acceptance criteria in tasks/mission.md are implemented, the verifier is green, and a fresh-context reviewer confirmed the diff.`
   - *Verifier* — the project's gate command(s): tests + lint + build, exit 0 = green.
   - *Max iterations* — finite, default 25.
2. **Dangerous-ops manifest.** Enumerate anticipated permission gates and
   irreversible ops (pushes, merges, deletions, deploys, network egress).
   Present ONE upfront approval request to the user. Anything NOT on the
   approved manifest still stops for the user later. This converts mid-run
   "should I continue?" permission stalls into a single pre-flight answer.
3. **Write `tasks/mission.md`** (planning-with-files format): goal,
   acceptance criteria (checkboxes), verifier command, sigil statement,
   approved manifest, cancel instructions
   (`rm .claude/ralph-loop.local.md` or `/cancel-ralph`). Also create an
   empty `tasks/progress.md` now — doctrine and the loop prompt tell every
   iteration to read it, so it must exist from iteration 1. Seed
   `tasks/session-constraints.md` with any durable user directives already
   given this session (see Constraint pinning below); create it empty if
   none.
4. **Arm the loop** — run from the WORKTREE ROOT (state file is CWD-scoped).
   Set `SIGIL` to the mission-specific statement from mission.md first —
   NEVER pass a placeholder: an unsubstituted promise can never match, so
   the loop would only end at max-iterations. Sigil rules: single line, no
   double quotes (the setup script YAML-wraps it verbatim), TRUE only at
   genuine completion.

   ```bash
   # Preflight: only arm inside a dedicated worktree. The state file traps
   # ALL sessions that stop in this directory (empty session_id fallthrough).
   case "$PWD" in
     "$HOME"|"$HOME/.claude"|"$HOME/Repos") echo "REFUSE: arm only from a dedicated worktree root"; exit 1;;
   esac
   [[ -e .git ]] || { echo "REFUSE: not a repo/worktree root"; exit 1; }  # .git is a FILE in linked worktrees, a dir in main checkouts; -e covers both

   mkdir -p tasks && : > tasks/progress.md   # must exist from iteration 1 (doctrine reads it)
   SIGIL='All acceptance criteria in tasks/mission.md are implemented, the verifier is green, and a fresh-context reviewer confirmed the diff.'
   case "$SIGIL" in *'"'*|*'$'*|*'`'*|*'\'*)
     echo 'REFUSE: sigil must not contain " $ ` or \ (setup script YAML-wraps it verbatim; a violation makes the sigil unmatchable and the loop runs to max-iterations)'; exit 1;;
   esac
   SETUP=$(ls -d ~/.claude/plugins/cache/claude-plugins-official/ralph-loop/*/scripts/setup-ralph-loop.sh 2>/dev/null | sort -V | tail -1)
   [[ -f "$SETUP" ]] || { echo "REFUSE: ralph-loop plugin not found in plugin cache — loop would silently never arm"; exit 1; }
   "$SETUP" "Autonomous mission: read tasks/mission.md, tasks/session-constraints.md, and tasks/progress.md, then continue from where progress.md left off (empty progress.md = start at the first unchecked acceptance criterion). Orchestrate via subagents; do not implement inline; paste session-constraints.md verbatim into every subagent briefing." \
     --completion-promise "$SIGIL" --max-iterations 25
   [[ -f .claude/ralph-loop.local.md ]] || { echo "ARM FAILED: state file missing — stop gate is NOT live"; exit 1; }
   ```

   Caveat: armed via safe-bash, `session_id` in the state file is empty, so
   the stop gate applies to ANY session in this directory — dedicated
   worktrees are the isolation boundary; the preflight guard above enforces
   the obvious misfires. Never arm in a shared repo dir, and run ONE
   orchestrator session per worktree (two sessions stopping concurrently
   race the state file's read-modify-write). Subagents are safe: the plugin
   registers only the `Stop` event, which fires when the MAIN agent
   finishes responding — subagent completion is the separate
   `SubagentStop` event and never triggers the gate (verified against
   official hooks docs 2026-08-02).

## Sigil discipline — verifier earns the sigil, sigil earns the stop

- The ONLY legitimate stop is `<promise><sigil statement></promise>`,
  emitted ONLY when the statement is literally TRUE:
  1. every acceptance criterion in mission.md checked off;
  2. verifier freshly run this iteration, exit 0;
  3. fresh-context reviewer (close-out advice-refine-test-loop) confirms
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
- Emitting the sigil without a fresh verifier run + reviewer confirmation
- Parent Reading source files "just to check" — dispatch a subagent
- Asking the user mid-loop anything mission.md or the approved manifest
  already answers
- Briefing a subagent with a paraphrase of user constraints instead of
  pasting `tasks/session-constraints.md` verbatim — paraphrase is how
  constraints silently die in handoff

## Scope guard

Default for anything you'd branch for. NOT for trivial edits or quick
questions — subagents and branches are pure overhead there. Existing
"pick the simpler option / don't scope-creep" rules still govern.
````

- [ ] **Step 2: Verify the skill parses**

Run: `head -6 /home/jonathan/.claude/skills/autonomous-orchestration/SKILL.md`
Expected: frontmatter intact (`---`, `name:`, `description:`, `---`). Section sanity (whitelist, not brittle count):

```bash
for s in 'Phase 0 — Autonomy contract' 'Sigil discipline' 'Context discipline'; do
  grep -qF "## $s" /home/jonathan/.claude/skills/autonomous-orchestration/SKILL.md || { echo "MISSING: $s"; exit 1; }
done && echo OK
```

- [ ] **Step 3: Commit**

```bash
cd /home/jonathan/.claude
git add skills/autonomous-orchestration/SKILL.md
git commit -m "feat(autonomous-orchestration): Phase 0 autonomy contract, sigil discipline, context discipline (Path B)"
```

---

### Task 4: E2E integration smoke — our contract vs the official plugin

Validates the assumptions Tasks 1-3 bake in: state-file format, empty-session fallthrough, sigil detection, max-iterations stop. Read-only against the plugin; sandboxed in /tmp. **Run all Task 4 steps in ONE shell session** — the EXIT trap and `$SANDBOX`/`$SETUP`/`$STOPHOOK`/`$TRANSCRIPT` variables from Step 1 carry across steps.

**Files:**
- None created in repo (throwaway /tmp sandbox). Findings appended to plan doc if surprises.

- [ ] **Step 1: Arm via official setup script in a sandbox**

```bash
SANDBOX=$(mktemp -d) && cd "$SANDBOX"
trap 'rm -rf "$SANDBOX"' EXIT
SETUP=$(ls -d ~/.claude/plugins/cache/claude-plugins-official/ralph-loop/*/scripts/setup-ralph-loop.sh | sort -V | tail -1)
"$SETUP" "test mission" --completion-promise 'SIGIL TRUE' --max-iterations 2
head -8 .claude/ralph-loop.local.md
```

Expected: state file exists; frontmatter has `iteration: 1`, `max_iterations: 2`, `completion_promise: "SIGIL TRUE"`, empty `session_id:`.

- [ ] **Step 2: Stop WITHOUT sigil → hook blocks and re-feeds prompt**

```bash
STOPHOOK=$(ls -d ~/.claude/plugins/cache/claude-plugins-official/ralph-loop/*/hooks/stop-hook.sh | sort -V | tail -1)
TRANSCRIPT="$SANDBOX/t.jsonl"
printf '%s\n' '{"role":"assistant","message":{"content":[{"type":"text","text":"still working, no sigil"}]}}' > "$TRANSCRIPT"
# Deliberate session mismatch: state session_id is EMPTY (armed via safe-bash,
# no CLAUDE_CODE_SESSION_ID in MCP env), so stop-hook's isolation check
# `[[ -n "$STATE_SESSION" ]] && [[ "$STATE_SESSION" != "$HOOK_SESSION" ]]`
# is false on the -n test and FALLS THROUGH to blocking. This is the
# documented legacy behavior our worktree-isolation mitigation depends on —
# do NOT "fix" the fallthrough thinking it's a bug.
echo "{\"session_id\":\"whatever\",\"transcript_path\":\"$TRANSCRIPT\"}" | bash "$STOPHOOK"
grep -q '^iteration: 2$' .claude/ralph-loop.local.md && echo ITER-BUMPED || echo FAIL-NO-BUMP
```

Expected: JSON with `"decision": "block"` and `"reason"` containing `test mission`; then `ITER-BUMPED`.

- [ ] **Step 3: Stop WITH sigil → hook releases and removes state**

```bash
printf '%s\n' '{"role":"assistant","message":{"content":[{"type":"text","text":"done. <promise>SIGIL TRUE</promise>"}]}}' > "$TRANSCRIPT"
echo "{\"session_id\":\"whatever\",\"transcript_path\":\"$TRANSCRIPT\"}" | bash "$STOPHOOK"
ls .claude/ralph-loop.local.md 2>&1 || echo REMOVED
```

Expected: `✅ Ralph loop: Detected <promise>SIGIL TRUE</promise>`; `REMOVED`.

- [ ] **Step 4: Max-iterations release**

```bash
"$SETUP" "test mission" --completion-promise 'SIGIL TRUE' --max-iterations 1
printf '%s\n' '{"role":"assistant","message":{"content":[{"type":"text","text":"no sigil"}]}}' > "$TRANSCRIPT"
echo "{\"session_id\":\"x\",\"transcript_path\":\"$TRANSCRIPT\"}" | bash "$STOPHOOK"
ls .claude/ralph-loop.local.md 2>&1 || echo REMOVED
```

Expected: `🛑 Ralph loop: Max iterations (1) reached.`; `REMOVED`.

- [ ] **Step 5: Manual-cancel release (`rm` path)**

```bash
"$SETUP" "test mission" --completion-promise 'SIGIL TRUE' --max-iterations 5
rm .claude/ralph-loop.local.md
printf '%s\n' '{"role":"assistant","message":{"content":[{"type":"text","text":"anything"}]}}' > "$TRANSCRIPT"
echo "{\"session_id\":\"x\",\"transcript_path\":\"$TRANSCRIPT\"}" | bash "$STOPHOOK"; echo "exit=$?"
```

Expected: no block JSON, `exit=0` — removing the state file is a working kill switch (this is the documented cancel path; `/cancel-ralph` is the in-session equivalent). Sandbox cleans itself via the EXIT trap.

- [ ] **Step 6: Record outcome**

If any expectation fails, STOP and fix the corresponding assumption in Task 1/3 before proceeding (they encode this contract). On success, no commit needed.

---

### Task 5: Memory + close-out

**Files:**
- Create: `/home/jonathan/.claude/projects/-home-jonathan-Repos/memory/path-b-sigil-orchestration.md`
- Modify: `/home/jonathan/.claude/projects/-home-jonathan-Repos/memory/MEMORY.md`

- [ ] **Step 1: Write project memory**

```markdown
---
name: path-b-sigil-orchestration
description: Autonomous runs use ralph-loop sigil gate (Path B, 2026-08-02) — not the parked continue-nudge judge
metadata:
  type: project
---

Path B shipped 2026-08-02: autonomous-orchestration Phase 0 arms the official
ralph-loop plugin (sigil `<promise>…</promise>` + max-iterations) with a
verification-gated sigil; `compact-reinject.sh` (SessionStart
startup|compact|clear) re-injects doctrine while a loop is armed; durable
user directives are pinned datestamped to `tasks/session-constraints.md` at
directive-time and pasted verbatim into every subagent briefing (compaction
kills zero-artifact constraints; disk beats context).

**Why:** FM1 (voluntary deferral) — deterministic sigil gate beats the LLM
judge (continue-nudge measured 47% precision); FM2 (context-cap loss) —
file-based handoff (mission.md/progress.md) + doctrine re-injection is
Anthropic's own answer to compaction loss. Continue-nudge pipeline
(`~/.claude/continue-nudge-*.py`) is parked, unwired, optional.

**How to apply:** Autonomous multi-chunk work → autonomous-orchestration skill
Phase 0 (never skip arming). Don't propose reviving continue-nudge as the stop
gate; it's superseded for autonomous runs.
```

- [ ] **Step 2: Add MEMORY.md index line**

Append: `- [Path B sigil orchestration](path-b-sigil-orchestration.md) — autonomous runs gate stops via ralph-loop sigil; continue-nudge parked`

- [ ] **Step 3: Final verification + commit**

Run both test scripts once more (`test-compact-reinject.sh` → PASS; `jq empty settings.json` → OK), then:

```bash
cd /home/jonathan/.claude
git add docs/superpowers/plans/2026-08-02-sigil-gated-autonomous-orchestration.md projects/-home-jonathan-Repos/memory/path-b-sigil-orchestration.md projects/-home-jonathan-Repos/memory/MEMORY.md
git commit -m "docs: Path B plan + project memory (sigil-gated autonomous orchestration)"
```

---

### Task 6: Session-constraints integration (added 2026-08-02 after user review-improvements input; execute after Task 3 and BEFORE Task 5, so the memory close-out describes machinery that already exists)

Constraints die three ways: compaction drops zero-artifact directives, attention decays over long sessions, and subagent briefings lose whatever slipped the parent's summary. A pinned constraints file fixes all three — disk beats context. This task wires `tasks/session-constraints.md` into the doctrine (hook script) and the global CLAUDE.md checkpoint convention. (The SKILL.md side — Constraint pinning bullet, Phase 0 seeding, recitation, red flag — is already in Task 3's replacement content.)

**Files:**
- Modify: `/home/jonathan/.claude/skills/autonomous-orchestration/hooks/test-compact-reinject.sh`
- Modify: `/home/jonathan/.claude/skills/autonomous-orchestration/hooks/compact-reinject.sh`
- Modify: `/home/jonathan/.claude/CLAUDE.md` (Context checkpoint section)

- [ ] **Step 1: Extend the test (TDD — new assertion first)**

In `test-compact-reinject.sh`, Case 2 block, add after the `tasks/mission.md` assertion:

```bash
echo "$OUT" | grep -q 'tasks/session-constraints.md'               || { echo "FAIL: missing session-constraints pointer"; exit 1; }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bash /home/jonathan/.claude/skills/autonomous-orchestration/hooks/test-compact-reinject.sh`
Expected: `FAIL: missing session-constraints pointer`, non-zero exit.

- [ ] **Step 3: Update the doctrine line in compact-reinject.sh**

Replace the first doctrine bullet:

```
- Re-read tasks/mission.md and tasks/progress.md NOW. Trust files and git log over any memory of this conversation.
```

with:

```
- Re-read tasks/mission.md, tasks/progress.md, and tasks/session-constraints.md NOW. Constraints are the first thing compaction kills — the file is authoritative. Trust files and git log over any memory of this conversation.
```

- [ ] **Step 4: Run test to verify it passes**

Run the test. Expected: `PASS`, exit 0. `bash -n` both files.

- [ ] **Step 5: Extend CLAUDE.md Context checkpoint section**

In `/home/jonathan/.claude/CLAUDE.md`, the paragraph beginning `**Context checkpoint.**` — append to that paragraph:

```markdown
Alongside it, maintain `tasks/session-constraints.md` (any project, any session — create the file on first use): the moment the user states a directive that outlives the current task ("don't X", "always Y", "never Z", "keep it A"), append it datestamped in that same turn — directive-time is when drift risk is lowest. Tasks are not constraints ("fix the typo" is a task). Re-read the file after every compact/resume, and whenever you brief a subagent, paste its contents verbatim into the briefing — constraints propagate mechanically, never by paraphrase. Compaction kills zero-artifact directives; disk beats context.
```

(Global placement in CLAUDE.md is user-mandated — the pinning convention applies to all sessions, not just autonomous loops; the wording above is written to be coherent when no subagents or tasks/ dir exist yet.)

- [ ] **Step 6: Commit**

```bash
cd /home/jonathan/.claude
git add skills/autonomous-orchestration/hooks/compact-reinject.sh skills/autonomous-orchestration/hooks/test-compact-reinject.sh CLAUDE.md
git commit -m "feat(autonomous-orchestration): session-constraints.md pinning — doctrine re-read + global checkpoint convention"
```

---

## Deliberately out of scope (YAGNI, revisit only on demand)

- Custom layered Stop hook (never-nudge regex → deferral regex → sigil): superseded — the official plugin blocks ALL stops while armed, which is strictly stronger; anti-deferral language lives in the loop prompt + re-injected doctrine instead.
- Anthropic context-editing/memory tool via Agent SDK: different runtime (SDK, not CLI); separate project.
- Custom OpenHands-style compaction prompt: no supported Claude Code config surface for the compaction prompt; mission.md/progress.md recitation + compact re-injection covers the loss mode.
- Continue-nudge revival for interactive (non-autonomous) sessions: parked pending sigil-pattern results.
- Auto-pinning constraints via a hook that regexes user messages for "don't/never/stop": rejected (user's own analysis) — can't distinguish "don't worry about it" from real pins; false constraints poison trust in the file. Precision-first (write at directive-time), recall-backstop-second (corrections loop catches repeat violations).
- Refusing re-injection when `completion_promise: null` + `max_iterations: 0` (advisor suggestion): rejected — our Phase 0 always sets both, and if a user hand-arms that combo, injecting doctrine WITH the stale-loop cancel instructions aids discoverability; silent refusal would hide the trap.
- Em-dash headings flagged as copy-paste risk: verified non-issue — repo is UTF-8 throughout and the existing SKILL.md already uses em-dashes.
- "Subagent Task stops trigger the ralph Stop hook and get trapped in the loop" (adversarial round): REFUTED — official hooks docs list `Stop` ("when Claude finishes responding") and `SubagentStop` ("when a subagent finishes") as distinct events; the plugin registers only `Stop`.
- "Stop hook times out on large transcripts → fail-open" (adversarial round): REFUTED empirically — 227MB compact-JSONL transcript processed in 0.22s (grep streams; jq input capped at last 100 assistant lines). Side-finding: the hook's `grep '"role":"assistant"'` requires compact JSON (no space after colons), which matches real transcript format.
