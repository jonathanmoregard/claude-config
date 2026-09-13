# Task-scoped definition-of-done implementation plan

> **For agentic workers:** Keep every edit inside
> `~/worktrees/claude-dod-task-scope`; live `~/.claude` stays untouched. Follow
> test-driven development: observe each new behavioral test fail before adding
> production code.

**Goal:** Give an interactive user one explicit task-boundary command that
retires stale DoD scope without letting ordinary prompts release long work.

**Architecture:** Add a narrow `UserPromptSubmit` state-transition hook. It
validates user-originated reset syntax, refuses live Ralph ownership, serializes
against the existing arm hook through one stable lock, audits retired repos,
then clears only the calling session's retry and arm files. Keep Stop/checker
logic and terminal outcomes unchanged.

**Verifier:** `bash scripts/tests/stop-hooks/run.sh`

---

### Task 1: Pin task-boundary behavior with failing interaction tests

**Files:**

- Modify: `scripts/tests/stop-hooks/run.sh`
- Test target: `hooks/finishing-up-task-scope.py`
- Test target: `hooks/finishing-up-arm.sh`

- [ ] Route both task-scope and arm hooks through `UNDER_TEST_ROOT`, never live
  `~/.claude`.
- [ ] Add payload helpers that capture stdout and exit status without losing
  status across command substitution.
- [ ] Prove canonical `Reset DoD to new task: …` removes calling session's
  `.blocks` and `.arm`, reports success, and audits de-duplicated prior repos.
- [ ] Prove motivating `Reset dod skill to new task: …` syntax works.
- [ ] Prove ordinary prompts, empty descriptions, mid-prompt phrases, and known
  non-user sources leave state untouched.
- [ ] Prove another session's arm/count state remains untouched.
- [ ] Prove a live Ralph owner refuses reset and leaves state untouched.
- [ ] Prove reset waits on `.task-scope.lock` rather than racing through it.
- [ ] Prove arm hook also waits on the stable lock, then records its repo.
- [ ] Run focused suite and record expected RED failures from missing hook and
  missing arm serialization.
- [ ] Commit test contract.

### Task 2: Implement reset state transition

**Files:**

- Create: `hooks/finishing-up-task-scope.py`
- Test: `scripts/tests/stop-hooks/run.sh`

- [ ] Parse JSON defensively; allow only a plain session token and exact
  beginning-anchored reset syntax with non-empty task description.
- [ ] Ignore payloads with an explicit non-user source.
- [ ] Refuse non-empty background ownership fields and a live Ralph state file.
- [ ] Create state directory safely and acquire `.task-scope.lock` with a
  bounded wait.
- [ ] Read/de-duplicate current arm repositories and append a bounded,
  prompt-free audit record using one `O_APPEND` write.
- [ ] Reset `.blocks` before `.arm`; keep old arm active on any failure and emit
  visible `UserPromptSubmit` context.
- [ ] Emit concise success context; do not create an armfile for a read-only new
  task.
- [ ] Run Python compile and focused Stop-hook suite to GREEN.
- [ ] Commit implementation.

### Task 3: Serialize arming against task reset

**Files:**

- Modify: `hooks/finishing-up-arm.sh`
- Test: `scripts/tests/stop-hooks/run.sh`

- [ ] Open and acquire the same stable lock before touching a session armfile.
- [ ] Fail loudly and non-blockingly if stable lock support or acquisition
  fails; never report an unrecorded edit as armed.
- [ ] Preserve current repo resolution, exclusions, default-branch support,
  de-duplication, and failure messaging.
- [ ] Run `bash -n`, focused suite, and `git diff --check`.
- [ ] Commit arm synchronization.

### Task 4: Wire and document explicit scope reset

**Files:**

- Modify: `settings.json`
- Modify: `skills/finishing-up/SKILL.md`
- Test: `scripts/tests/stop-hooks/run.sh`

- [ ] Register reset hook under `UserPromptSubmit` with a five-second timeout.
- [ ] Add reset phrasing to `finishing-up` skill description so direct requests
  trigger the skill.
- [ ] Document one-active-scope semantics, canonical command, Ralph refusal,
  cross-session isolation, and automatic re-arming after later edits.
- [ ] Do not describe reset as shipping, deletion authority, or a persistent
  exemption.
- [ ] Parse settings JSON, run skill/config checks, and rerun focused suite.
- [ ] Commit wiring and docs.

### Task 5: Integrated review and delivery

**Files:**

- Verify all changed files and task notes.

- [ ] Run Stop-hook suite, Python compile, Bash syntax, settings JSON parse,
  relevant smoke/config checks, and `git diff --check`.
- [ ] Invoke fresh-context review through the required review workflow; reproduce
  and fix every material finding before rerunning checks.
- [ ] Update mission evidence and acceptance boxes.
- [ ] Commit remaining docs, push owned branch, open a PR against `master`, and
  drive checks to green/mergeable.
- [ ] Measure exact final state with `python3 ~/.claude/scripts/dod-check.py
  --repo ~/worktrees/claude-dod-task-scope`.
