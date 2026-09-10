# DoD terminal outcomes implementation plan

> **For agentic workers:** Follow test-driven development. Keep edits inside
> `~/worktrees/claude-dod-terminal-outcomes`; live `~/.claude` stays untouched.

**Goal:** Keep DoD nudges active until work measures clean or the agent gives
the user one valid, bounded terminal explanation.

**Architecture:** Keep `dod-check.py` as the shipping-readiness predicate. Use
runtime-owned `stop_hook_active` only to scope a strict closing envelope to the
current continuation, parse synchronous `last_assistant_message`, and repeat
measurement when no valid explanation exists. Teach `finishing-up` the same
finite outcome contract.

**Verifier:** `bash scripts/tests/stop-hooks/run.sh`

---

### Task 1: Pin explanatory exit and message contracts

**Files:**
- Modify: `scripts/tests/stop-hooks/run.sh`

- [ ] Add a `GATE_UNDER_TEST` override and isolated fake home that routes both
  direct and Ralph-integrated calls through this branch's hook/scripts; never
  let a green result come from live `~/.claude`.
- [ ] Replace the blanket re-entry assertion with table-driven JSON-envelope
  cases for QUESTION, BLOCKED, PRESERVE, and ABANDON.
- [ ] Reject malformed JSON, missing/extra/wrong-typed fields, unknown outcome,
  mismatched aggregate state, quoted/non-final marker, non-string final message,
  and a marker replayed without runtime re-entry.
- [ ] Arm two blocked repositories and reject missing, duplicate, unknown, or
  already-clean repo records; accept complete exact coverage.
- [ ] Prove a valid exit resets the current block count without deleting the
  armfile, so a later user turn measures again with a fresh budget.
- [ ] Make the countfile non-resettable and prove the exit blocks with the reset
  error instead of allowing at a stale spent budget.
- [ ] Prove false, absent, and wrong-shaped re-entry values do not bypass
  measurement.
- [ ] Prove explicit Ralph handover still measures even when the re-entry flag
  is true.
- [ ] Capture an ordinary block reason and require Ship, Preserve, Abandon,
  PR-only-for-delivery, explicit deletion approval, conditional PR URL, and a
  single reporting follow-up.
- [ ] Reject old “drive to green and mergeable” and unconditional clickable-PR
  wording.
- [ ] Run focused suite and record expected RED failures.
- [ ] Commit test contract.

### Task 2: Implement bounded explanatory exits

**Files:**
- Modify: `hooks/finishing-up-gate.py`
- Test: `scripts/tests/stop-hooks/run.sh`

- [ ] Add a small parser for the final two non-empty lines of string
  `last_assistant_message`; parse one-line JSON with an exact top-level schema,
  exact per-outcome keys and non-empty string values, then calculate the required
  aggregate state marker.
- [ ] After objective measurement, require the envelope's repo set to equal the
  currently blocked repo set exactly, with no duplicates.
- [ ] On ordinary runtime re-entry, allow only a valid envelope after a
  successful block-count reset; log outcome names without logging user prose.
  Otherwise continue to another bounded nudge with the parse, coverage, or reset
  error included.
- [ ] Never apply the parser to an explicit Ralph handover or an independent
  Stop where `stop_hook_active` is not exact boolean true.
- [ ] Replace blocker prose with concise Ship/Preserve/Abandon guidance.
- [ ] Include exact explanatory-exit syntax and state that Abandon grants no
  deletion authority and no exit persists into the next user turn.
- [ ] Keep exact checker command, one durable follow-up rule, no-unseen-push
  rule, merge authority, actual evidence, and bounded fallback.
- [ ] Run `python3 -m py_compile hooks/finishing-up-gate.py` and Stop-hook suite.
- [ ] Commit implementation.

### Task 3: Make finishing-up intent-aware

**Files:**
- Modify: `skills/finishing-up/SKILL.md`

- [ ] Remove process summary from description; retain concrete trigger terms.
- [ ] Add a first step that selects Ship, Preserve, or Abandon before blocker
  actions.
- [ ] Scope `NO_PR`, unpushed-commit, CI, and merge ladders to Ship.
- [ ] Define evidence/handover for Preserve and approval/target verification for
  Abandon.
- [ ] Define Question and Blocked terminal explanations and exact closing
  envelope/status mapping; Ship remains checker-controlled.
- [ ] State that a PR URL is included only when one exists.
- [ ] Run Markdown/diff checks and rerun pressure scenario with fresh agent.
- [ ] Commit skill change.

### Task 4: Integrated review and delivery

**Files:**
- Verify all changed files.

- [ ] Run full Stop-hook suite, syntax check, `git diff --check`, and repository
  skill/config checks relevant to changed files.
- [ ] Invoke one fresh-context advice/refine/test review; reproduce and fix any
  material finding, then rerun verifier.
- [ ] Update mission/progress evidence and acceptance checkboxes.
- [ ] Commit remaining documentation, push branch, open PR, and measure with
  `python3 ~/.claude/scripts/dod-check.py --repo <worktree>`.
- [ ] If checks remain pending, create one durable reporting follow-up; never
  merge automatically.
