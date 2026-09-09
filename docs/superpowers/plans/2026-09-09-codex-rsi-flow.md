# Codex RSI Flow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (default) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce cache-efficient Codex session reflections and feed bounded normalized reflections into enabled RSI review.

**Architecture:** `session-reflect` classifies source client and invokes either existing Claude fork-resume or a tool-less Codex resume against a secure copy of the rollout. Both paths emit one bounded artifact schema; nightly RSI reads only newest completed top-level artifacts.

**Tech Stack:** Bash, jq, Codex CLI JSONL, JSON Schema, offline shell harness, Python smoke tests.

---

### Task 1: Codex transcript and invocation contract

**Files:**
- Modify: `skills/session-reflect/tests/run.sh`
- Modify: `skills/session-reflect/lib.sh`
- Create: `skills/session-reflect/reflection.schema.json`

- [ ] Add a Codex rollout fixture containing `session_meta`, user/assistant
  `response_item` messages, encrypted reasoning, tool calls, and telemetry.
- [ ] Add failing assertions that client detection returns `codex`, legible
  messages survive, and encrypted reasoning/tool arguments/telemetry do not.
- [ ] Run `bash skills/session-reflect/tests/run.sh fast`; expect new assertions
  to fail because only Claude `.message.content` is parsed.
- [ ] Implement `reflect_transcript_client` plus client-aware JSONL extraction.
  Codex branch accepts message text and tool names/results only; it never falls
  back to a raw JSON excerpt.
- [ ] Add strict five-array output schema with `maxItems: 10` and string
  `maxLength: 500`.
- [ ] Re-run focused harness; expect parser assertions green.

### Task 2: Non-mutating cached Codex reflection

**Files:**
- Modify: `skills/session-reflect/tests/run.sh`
- Modify: `skills/session-reflect/reflect.sh`
- Modify: `skills/session-reflect/README.md`

- [ ] Add a stub `codex` executable recording argv, `CODEX_HOME`, prompt, and
  emitting `thread.started`, `item.completed`, and `turn.completed` JSONL.
- [ ] Add failing assertions: copied rollout exists during invocation, live
  rollout digest is unchanged, Claude is never called, tool-bearing features
  and hooks are disabled, same model is selected, and usage reaches artifact.
- [ ] Run focused harness; expect failure because session-end always invokes
  Claude.
- [ ] Implement `reflect_codex_call`: secure runtime scratch, validated relative
  rollout path, copied auth/rollout, `codex exec resume --ephemeral --all`, same
  source model, low effort, ignored user config/rules, disabled hooks/tools/apps,
  read-only sandbox, strict output schema, synthetic result envelope, exact
  scratch cleanup.
- [ ] Route only Codex `sessionend` through native call. Record and skip Codex
  `precompact`, `mission`, and `subagentstop` before spawning a worker.
- [ ] Re-run focused harness; expect all assertions green.

### Task 3: Hard-bound normalized artifacts

**Files:**
- Modify: `skills/session-reflect/tests/run.sh`
- Modify: `skills/session-reflect/reflect.sh`

- [ ] Add failing fixture returning non-string entries, more than ten entries,
  and strings over 500 characters.
- [ ] Assert persisted arrays contain only first ten strings, each at most 500
  characters, and artifact contains `source_client`.
- [ ] Run focused harness; expect cap assertions to fail.
- [ ] Extend normalization filter to select strings, cap strings and arrays, and
  attach `source_client` to artifacts/manifests.
- [ ] Re-run focused harness; expect all assertions green.

### Task 4: Reflection-backed RSI review

**Files:**
- Modify: `tests/test_proposals_sink.py`
- Modify: `recursive-self-improvement/config/prompt.md`
- Modify: `recursive-self-improvement/config/config.json`

- [ ] Add failing static contract test requiring prompt to use top-level
  `~/.claude/reflections/sessions/*/final.json`, last 24 hours, newest 12,
  completed session-end status, subagent exclusion, and source-path no-follow.
- [ ] Run focused unittest; expect failure because prompt scans Claude logs.
- [ ] Replace raw-log instructions with bounded normalized-reflection input and
  update proposal source guidance.
- [ ] Flip `components.daily_review` from boolean false to boolean true.
- [ ] Re-run focused unittest; expect green.

### Task 5: Verification and delivery

**Files:**
- Verify all changed files

- [ ] Run `bash skills/session-reflect/tests/run.sh fast`; expect zero failures.
- [ ] Run `python3 -m unittest tests.test_proposals_sink`; expect zero failures.
- [ ] Run `bash tests/smoke.sh`; expect zero failures.
- [ ] Run `bash -n skills/session-reflect/reflect.sh skills/session-reflect/lib.sh skills/session-reflect/tests/run.sh`.
- [ ] Run `jq -e . skills/session-reflect/reflection.schema.json recursive-self-improvement/config/config.json`.
- [ ] Run `git diff --check` and secret scan.
- [ ] Review security boundaries and non-mutating cache probe evidence.
- [ ] Commit focused changes, push `feat/codex-rsi-flow`, and open PR against
  `master`.
