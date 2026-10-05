# Native Claude Subagent Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route exploration to Haiku, routine implementation to Sonnet, hard implementation to Opus, and advisory review to Fable without relying on ai-router model rewrites.

**Architecture:** Claude Code's native model precedence is the source of truth. `CLAUDE_CODE_SUBAGENT_MODEL` supplies the Sonnet default, pinned custom agents override it for Explore/easy/hard/advisory roles, and the ai-router remains an explicit cross-provider CLI only. A synchronous observation-only PostToolUse hook records the requested and resolved model without prompts or model calls.

**Tech Stack:** Claude Code 2.1.289 settings and custom-agent frontmatter, Python 3 standard-library hooks, pytest, agent-config-e2e.

---

### Task 1: Lock the routing contract with failing tests

**Files:**
- Create: `tests/test_native_agent_routing.py`
- Modify: `tests/test_ai_router_hooks.py`

- [ ] **Step 1: Write failing settings and agent-definition tests**

Assert that `settings.json` sets `CLAUDE_CODE_SUBAGENT_MODEL` to `claude-sonnet-5-5`, does not wire `ai-router-agent-gate.py` under PreToolUse, and wires a new observation-only PostToolUse hook. Assert exact pinned IDs for `Explore`, `worker-easy`, `worker-hard`, and the existing `advisor`.

- [ ] **Step 2: Write failing observer tests**

Drive the hook as a subprocess with representative Agent results and require JSONL records containing only metadata: agent type/id, requested model, resolved model, models used, expected family, and mismatch status. Include parallel-safe append and malformed-input fail-open cases.

- [ ] **Step 3: Run the targeted tests and verify RED**

Run:

```bash
nix shell nixpkgs#python3Packages.pytest -c pytest -q \
  tests/test_native_agent_routing.py tests/test_ai_router_hooks.py
```

Expected: failures for the absent settings, agents, observer, and obsolete router-gate wiring.

### Task 2: Implement native routing

**Files:**
- Modify: `settings.json`
- Create: `agents/Explore.md`
- Create: `agents/worker-easy.md`
- Create: `agents/worker-hard.md`
- Modify: `CLAUDE.md`
- Modify: `skills/ai-router/SKILL.md`

- [ ] **Step 1: Add the native default and role agents**

Set the non-forced default model to `claude-sonnet-5-5`. Pin Explore to `claude-haiku-4-5-20251001`, easy work to `claude-sonnet-5-5`, and hard work to `claude-opus-5-5`. Keep the existing advisor pin at `claude-fable-5-1`.

- [ ] **Step 2: Make role selection explicit and short**

Add one concise routing table to `CLAUDE.md`: Explore for read-only discovery, worker-easy for localized known-pattern work, worker-hard for cross-cutting/architectural work, advisor for read-only review. State that forks inherit the main model and are reserved for continuity-heavy work.

- [ ] **Step 3: Narrow ai-router's contract**

Remove the automatic Agent-gate claim from the skill. Keep `pick`/`delegate` for deliberate cross-provider work and quota escape only; native Claude-to-Claude routing uses the named agents.

- [ ] **Step 4: Parse after every edit**

Run `python3 -c "import json; json.load(open('settings.json'))"`, inspect agent frontmatter, and run `git diff --check`.

### Task 3: Add resolved-model evidence

**Files:**
- Create: `hooks/native-agent-route-observer.py`
- Modify: `settings.json`
- Test: `tests/test_native_agent_routing.py`

- [ ] **Step 1: Implement the minimal observer**

Read PostToolUse JSON from stdin, discard prompts and tool output, derive the expected family from the named role, and append one locked JSON line to `$AI_ROUTER_STATE/native-agent-routes.jsonl` (default `~/.local/state/ai-router`). Print nothing and fail open.

- [ ] **Step 2: Verify GREEN**

Run the targeted pytest command from Task 1, then the complete ai-router hook suite:

```bash
nix shell nixpkgs#python3Packages.pytest -c pytest -q \
  tools/ai-router/tests tests/test_ai_router_hooks.py \
  tests/test_ai_router_health.py tests/test_native_agent_routing.py
```

### Task 4: Prove actual models with a staged Claude session

**Files:**
- Create: `tests/agent-e2e/native-agent-routing/prompt.md`
- Create: `tests/agent-e2e/native-agent-routing/setup.sh`
- Create: `tests/agent-e2e/native-agent-routing/check.sh`

- [ ] **Step 1: Build a two-route scenario**

Ask a real headless parent to delegate one read-only discovery task and one routine implementation task. Grade the Agent tool results and subagent transcripts, not the parent's prose: Explore must resolve to Haiku and the easy worker to Sonnet.

- [ ] **Step 2: Probe the new arm once**

Run new only with `--n 1`, inspect the transcript for sandbox or grading artifacts, and fix the scenario rather than counting a false pass.

- [ ] **Step 3: Compare old and new**

Run one fixed old/new batch against `origin/master` and the candidate commit. Report every INCONCLUSIVE and do not replace it. Because the weekly quota has only 9% headroom, use the smallest batch that proves the deterministic model-resolution change.

### Task 5: Verify and deliver

**Files:**
- Modify only files named above.

- [ ] **Step 1: Run static and full relevant checks**

```bash
python3 -m py_compile hooks/native-agent-route-observer.py hooks/ai-router-*.py
python3 -c "import json; json.load(open('settings.json'))"
nix shell nixpkgs#python3Packages.pytest -c pytest -q \
  tools/ai-router/tests tests/test_ai_router_hooks.py \
  tests/test_ai_router_health.py tests/test_native_agent_routing.py
git diff --check
```

- [ ] **Step 2: Commit through the Git broker and run agent e2e old vs new**

The e2e arms must be refs. Commit the candidate, run the scenario, and record the generated summary path and pass table.

- [ ] **Step 3: Push and follow repository PR policy**

Check repository visibility and PR-triggered workflows. Push the branch. If PR-gated, ask before opening; otherwise create the PR with the root-cause metrics, research basis, unit-test results, and Agent e2e table.
