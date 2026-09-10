# DoD terminal outcomes implementation plan

> **For agentic workers:** Follow test-driven development. Keep edits inside
> `~/worktrees/claude-dod-terminal-outcomes`; live `~/.claude` stays untouched.

**Goal:** Stop repeated DoD continuation loops and prevent rejected or
intentionally retained work from being pushed merely to make the checker green.

**Architecture:** Keep `dod-check.py` as the shipping-readiness predicate. Add
the runtime-owned Stop re-entry guard to `finishing-up-gate.py`, replace its
single-outcome prompt with a concise three-outcome contract, and teach
`finishing-up` to choose intent before applying blocker fixes.

**Verifier:** `bash scripts/tests/stop-hooks/run.sh`

---

### Task 1: Pin Stop re-entry and message contracts

**Files:**
- Modify: `scripts/tests/stop-hooks/run.sh`

- [ ] Add an armed-session test proving boolean `stop_hook_active: true`
  returns no decision, exits zero, and does not spend the block budget.
- [ ] Prove false, absent, and wrong-shaped values do not bypass measurement.
- [ ] Capture an ordinary block reason and require Ship, Preserve, Abandon,
  PR-only-for-delivery, explicit deletion approval, conditional PR URL, and a
  single reporting follow-up.
- [ ] Reject old “drive to green and mergeable” and unconditional clickable-PR
  wording.
- [ ] Run focused suite and record expected RED failures.
- [ ] Commit test contract.

### Task 2: Implement one-continuation Stop behavior

**Files:**
- Modify: `hooks/finishing-up-gate.py`
- Test: `scripts/tests/stop-hooks/run.sh`

- [ ] After valid session/arm lookup, allow only when
  `payload.get("stop_hook_active") is True`; log the re-entry stand-down.
- [ ] Replace blocker prose with concise Ship/Preserve/Abandon guidance.
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

