# Codex Sync Ownership Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (default) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent Claude and Codex agents from hand-editing `~/.codex`, while directing every configuration change to Claude source or the `ai-client-config` renderer.

**Architecture:** Extend the existing mirrored file-write guard: Claude invokes it directly for Write/Edit-family tools, while `ai-client-config` adapts Codex `apply_patch` targets onto the same contract. Keep Codex runtime storage writable because only agent tool calls, not Codex internal writes or the sync service, pass through this guard. Put the durable route in `CLAUDE.md` so sync reproduces it in Codex `AGENTS.md`.

**Tech Stack:** Python 3 standard library, pytest, Claude PreToolUse hook JSON, Markdown configuration.

---

### Task 1: Specify `~/.codex` refusal behavior

**Files:**
- Modify: `tests/test_dot_claude_config_write_guard.py`

- [ ] **Step 1: Add Codex-root fixtures and actionable-reason helper**

Add `CODEX = os.path.join(os.path.expanduser("~"), ".codex")`. Keep
`assert_denied` for the existing Claude route and add:

```python
def assert_codex_denied(payload):
    code, out = run(payload)
    assert code == 0
    assert out is not None
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "deny"
    reason = hso["permissionDecisionReason"]
    assert "~/.claude" in reason
    assert "ai-client-config" in reason
    assert "ai-client-config-codex-sync.service" in reason
    return reason
```

- [ ] **Step 2: Add denied-path and boundary tests**

Cover every file tool against `~/.codex/config.toml`, both config and runtime
paths, tilde spelling, a fake `$HOME`, sibling `~/.codex-backup`, and traversal
to `~/notes`:

```python
@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit", "NotebookEdit"])
def test_every_file_tool_refuses_codex_tree(tool):
    assert_codex_denied(write_payload(os.path.join(CODEX, "config.toml"), tool=tool))

@pytest.mark.parametrize("rel", ["config.toml", "AGENTS.md", "skills/x/SKILL.md", "auth.json", "sessions/x.jsonl"])
def test_all_live_codex_paths_are_agent_read_only(rel):
    reason = assert_codex_denied(write_payload(os.path.join(CODEX, rel)))
    assert rel in reason

def test_codex_tilde_spelling_is_refused():
    assert_codex_denied(write_payload("~/.codex/config.toml"))

def test_codex_root_tracks_fake_home(tmp_path):
    target = tmp_path / ".codex" / "config.toml"
    code, out = run(write_payload(str(target)), home=tmp_path)
    assert code == 0
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

def test_codex_lookalike_and_traversal_are_allowed():
    assert_allowed(write_payload(os.path.expanduser("~/.codex-backup/config.toml")))
    assert_allowed(write_payload(os.path.join(CODEX, "..", "notes", "x.md")))
```

- [ ] **Step 3: Run focused tests and verify RED**

Run:

```bash
python3 -m pytest tests/test_dot_claude_config_write_guard.py -q
```

Expected: new Codex tests fail with `expected a decision, got nothing`; existing
Claude tests pass.

- [ ] **Step 4: Commit test contract**

```bash
git add tests/test_dot_claude_config_write_guard.py
git commit -m "test: require agents to leave Codex state sync-owned"
```

### Task 2: Extend shared file-write guard

**Files:**
- Modify: `hooks/dot-claude-config-write-guard.py`
- Test: `tests/test_dot_claude_config_write_guard.py`

- [ ] **Step 1: Add Codex root and precise containment helper**

Add:

```python
CODEX_REASON = (
    "Refused: {rel} is under live ~/.codex. Agents must not hand-edit Codex "
    "configuration or runtime state. Change shared configuration through the "
    "~/.claude worktree/PR flow; change Codex-specific rendering in the "
    "ai-client-config repository. Let ai-client-config-codex-sync.service "
    "publish generated output."
)

def codex_root():
    return os.path.join(os.path.expanduser("~"), ".codex")

def relpath_under(path, root):
    if not path:
        return None
    abs_path = os.path.abspath(os.path.expanduser(str(path)))
    for base in {root, os.path.realpath(root)}:
        try:
            rel = os.path.relpath(abs_path, base)
        except ValueError:
            continue
        if rel == os.pardir or rel.startswith(os.pardir + os.sep):
            continue
        return rel
    return None
```

Use `relpath_under` inside existing Claude `guarded_relpath` so root-boundary
logic stays single-purpose and existing exemptions/tracked-file behavior stays
unchanged.

- [ ] **Step 2: Refuse Codex target before Claude target**

In `main`, after extracting target:

```python
    codex_rel = relpath_under(target, codex_root())
    if codex_rel is not None:
        reason = CODEX_REASON.format(rel=codex_rel)
    else:
        rel = guarded_relpath(target, config_root())
        if rel is None:
            return 0
        reason = REASON.format(rel=rel)
```

Render `reason` as existing structured deny. Update module docstring to name
both protected live roots and state that Codex internal writes bypass agent
file tools.

- [ ] **Step 3: Run syntax check and focused tests; verify GREEN**

Run:

```bash
python3 -m py_compile hooks/dot-claude-config-write-guard.py
python3 -m pytest tests/test_dot_claude_config_write_guard.py -q
```

Expected: all focused tests pass with no warnings.

- [ ] **Step 4: Commit implementation**

```bash
git add hooks/dot-claude-config-write-guard.py
git commit -m "fix: keep Codex tree sync-owned"
```

### Task 3: Publish ownership rule through Claude source

**Files:**
- Modify: `CLAUDE.md`
- Modify: `skills/settings-management/SKILL.md`

- [ ] **Step 1: Add concise agent directive to `CLAUDE.md`**

Immediately after `Config edits (~/.claude)`, add:

```markdown
## Generated Codex state (`~/.codex`)
- Never edit anything under `~/.codex` manually. Shared config/behavior changes belong in `~/.claude` via its worktree/PR flow; Codex-only rendering or mapping changes belong in the `ai-client-config` repo. Let `ai-client-config-codex-sync.service` publish generated output.
- Codex owns runtime files there (auth, sessions, history, caches, logs). Do not hand-edit those either.
```

- [ ] **Step 2: Make `settings-management` trigger and redirect on `.codex`**

Extend frontmatter description with `editing ~/.codex` and add a top section:

```markdown
## Never edit `~/.codex`

`~/.codex` is downstream output and Codex runtime state. Never hand-edit it.
For shared configuration, change `~/.claude` through the worktree/PR flow
below. For Codex-specific rendering or mappings, change the
`ai-client-config` repository. Let `ai-client-config-codex-sync.service`
publish the result.
```

- [ ] **Step 3: Run fast file checks**

Run:

```bash
git diff --check
rg -n 'Never edit anything under `~/.codex`|ai-client-config-codex-sync.service' CLAUDE.md skills/settings-management/SKILL.md
python3 -m pytest tests/test_dot_claude_config_write_guard.py -q
```

Expected: no whitespace errors; directive appears in both files; guard suite
remains green.

- [ ] **Step 4: Commit instructions**

```bash
git add CLAUDE.md skills/settings-management/SKILL.md
git commit -m "docs: route Codex config changes through sync sources"
```

### Task 4: Verify mirrored Codex behavior and deliver

**Files:**
- Verify: `hooks/dot-claude-config-write-guard.py`
- Verify: `tests/test_dot_claude_config_write_guard.py`
- Verify: `CLAUDE.md`
- Verify: `skills/settings-management/SKILL.md`

- [ ] **Step 1: Run complete relevant repository checks**

Run:

```bash
python3 -m pytest tests/test_dot_claude_config_write_guard.py tests/test_skill_matches_tools.py -q
python3 -m py_compile hooks/dot-claude-config-write-guard.py
git diff origin/master...HEAD --check
```

Expected: all tests pass, compile succeeds, diff check is clean.

- [ ] **Step 2: Exercise real hook inputs**

Pipe one Write payload for `~/.codex/config.toml` and one for a normal `/tmp`
file into the worktree hook. Expected: first produces structured deny naming
`~/.claude`, `ai-client-config`, and the sync service; second emits nothing and
exits 0.

- [ ] **Step 3: Verify renderer contract without modifying live `.codex`**

Use an isolated temporary `$HOME` with the current `ai-client-config`
`origin/main` checkout, copy this branch's `CLAUDE.md` and hook into its fake
`~/.claude`, run `python3 -B scripts/sync_codex.py`, then assert generated
`~/.codex/AGENTS.md` contains `Generated Codex state` and generated hook plan
adapts file-tool targets. Never aim the renderer at live `$HOME`.

- [ ] **Step 4: Inspect final diff and status**

Run:

```bash
git status --short --branch
git diff origin/master...HEAD --stat
git diff origin/master...HEAD -- CLAUDE.md skills/settings-management/SKILL.md hooks/dot-claude-config-write-guard.py tests/test_dot_claude_config_write_guard.py
```

Expected: only design, plan, test, hook, global instruction, and skill files
changed.

- [ ] **Step 5: Push and open PR against `master`**

```bash
git push -u origin feat/codex-sync-ownership
gh pr create --base master --title "fix: keep Codex config sync-owned" --body-file <prepared-pr-body>
```

Expected: PR URL. Mention NixOS PR #218 as deployment dependency; do not merge
through CLI.
