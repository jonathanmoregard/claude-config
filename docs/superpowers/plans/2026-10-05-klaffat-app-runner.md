# Klaffat app-runner MCP implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: use
> `superpowers:executing-plans` for this tightly coupled broker implementation.
> Keep every edit inside `/home/jonathan/worktrees/claude-klaffat-app-runner`;
> live `~/.claude` stays untouched. Follow test-driven development and observe
> each behavioral test fail before adding production code.

**Goal:** Let Codex safely run Klaffat, its mock-backed E2E tests, its Claude
Design exporter, and the existing real Google/optional Microsoft calendar
service through a host-side structured MCP tool.

**Architecture:** Register a stdlib-only stdio MCP server in the shared Claude
source config so `ai-client-config` renders it into Codex. The server validates
one Klaffat linked worktree and maps typed modes onto the repository's fixed
launchers. Mock app/E2E/export work runs in owned transient user-systemd units;
real providers delegate to the installed hardened `klaffat-local-google`
launcher and never cross its credential boundary.

**Tech stack:** Python 3 standard library, MCP JSON-RPC 2024-11-05, systemd user
transient units, Klaffat `scripts/e2e-run`, NixOS `klaffat-local-google`,
`unittest`/`pytest`.

**Design:**
`docs/superpowers/specs/2026-10-05-klaffat-app-runner-design.md`

---

### Task 1: Pin the structured surface and fail-closed policy

**Files:**

- Create: `mcps/klaffat-app-runner/test_server.py`
- Create: `mcps/klaffat-app-runner/config.json`
- Create: `mcps/klaffat-app-runner/server.py`

- [ ] Add RED tests requiring exactly one `klaffat_app` tool with operations
  `start`, `status`, `logs`, and `stop`; start modes are exactly `app`, `e2e`,
  `claude_design_export`, and `real_calendar`. Assert no raw command, argv,
  environment, port, executable, output-path, or unit-name field exists.
- [ ] Add RED tests for an exact-version policy schema. Missing, unknown,
  wrong-typed, relative, duplicate, and over-broad values must reject. `%t` in
  the user-bus path expands from `os.getuid()`, never caller environment.
- [ ] Add RED request-schema tests: operation-specific required/allowed fields,
  strict booleans and integers, bounded log tails, canonical run IDs, spec only
  for E2E, and rejection of unknown fields.
- [ ] Implement only enough policy/request validation and tool metadata to turn
  the tests GREEN.
- [ ] Run `python3 -m unittest mcps/klaffat-app-runner/test_server.py`; expect
  zero failures.
- [ ] Run `python3 -m py_compile mcps/klaffat-app-runner/server.py` and
  `python3 -m json.tool mcps/klaffat-app-runner/config.json >/dev/null`.
- [ ] Commit the test contract and minimal structured surface.

### Task 2: Validate Klaffat worktrees, specs, and private state

**Files:**

- Modify: `mcps/klaffat-app-runner/test_server.py`
- Modify: `mcps/klaffat-app-runner/server.py`

- [ ] Add RED fixtures for a Klaffat primary repository plus linked worktree.
  Require a canonical immediate `klaffat-*` child under the configured root,
  the expected Git common directory, and an allowed origin.
- [ ] Reject the primary checkout, normal clones, relative paths, symlink
  aliases, nested paths, lookalike names, unexpected origins, and linked
  worktrees from another repository.
- [ ] Add RED E2E spec tests. Accept only an existing regular
  `tests/e2e/tests/*.spec.ts`; reject traversal, absolute paths, nested paths,
  non-spec files, missing files, symlinks, and mode-field smuggling.
- [ ] Add RED state tests for private root creation/validation, atomic mode-0600
  run records, generated run IDs, unknown IDs, owner/mode mismatch, symlinked
  state, and malformed/unknown JSON fields.
- [ ] Implement validation without a shell. Read-only Git subprocesses set
  `GIT_OPTIONAL_LOCKS=0`; every other subprocess uses fixed argv.
- [ ] Run the focused unittest suite to GREEN, followed by compile and
  `git diff --check`.
- [ ] Commit validation and state ownership.

### Task 3: Pin mode argv and lifecycle behavior

**Files:**

- Modify: `mcps/klaffat-app-runner/test_server.py`
- Modify: `mcps/klaffat-app-runner/server.py`
- Create: `mcps/klaffat-app-runner/hold.py`

- [ ] Add RED argv-construction tests for all modes. Assert fixed executable
  paths, no shell, no caller-controlled environment, derived unit/log paths,
  and exact mapping to `scripts/e2e-run`, `scripts/export-claude-design.sh`,
  and `klaffat-local-google`.
- [ ] Add RED start tests with fake systemd tools. App, E2E, and export must
  create an owned transient unit and durable state; failed acceptance must mark
  the run failed without claiming readiness.
- [ ] Add RED status tests for queued/active/ready/succeeded/failed/stopped,
  fixed mock-app URL discovery, E2E/export artifact discovery, and real-service
  status delegation.
- [ ] Add RED stop tests proving idempotence and exact owned-unit targeting.
  A real-calendar run may call only the fixed launcher's `stop`; unknown or
  tampered state must fail closed.
- [ ] Add RED singleton/concurrency tests for real-calendar starts and require
  the selected worktree to remain recorded while the trusted launcher builds.
- [ ] Implement lifecycle operations and the fixed signal-waiting helper.
  Never read the encrypted provider bundle, service environment, or `/proc`
  environment.
- [ ] Run focused unittest, compile both Python files, and `git diff --check`.
- [ ] Commit lifecycle behavior.

### Task 4: Bound logs, redact secrets, and support cancellation

**Files:**

- Modify: `mcps/klaffat-app-runner/test_server.py`
- Modify: `mcps/klaffat-app-runner/server.py`

- [ ] Add RED tests for exact tail bounds and output-size truncation.
- [ ] Add a mutation-style redaction table covering authorization/cookie
  headers, client secrets, access/refresh/id tokens, OAuth codes, query tokens,
  capability-bearing Klaffat URLs, and common assignment/JSON spellings. Prove
  ordinary URLs, exit states, and artifact paths remain useful.
- [ ] Add RED cancellation tests: tools run on worker threads, a cancellation
  suppresses the JSON-RPC response and terminates only the direct owned process
  group, and duplicate/missing request IDs cannot corrupt in-flight state.
- [ ] Implement bounded/redacted output and the MCP initialize/list/call/
  cancellation dispatcher.
- [ ] Run the focused suite and an end-to-end stdio protocol smoke to GREEN.
- [ ] Commit the MCP transport and disclosure boundary.

### Task 5: Register the shared server and pin repository contracts

**Files:**

- Modify: `mcp-servers.json`
- Create: `tests/test_klaffat_app_runner_asset.py`
- Test: `tests/test_mcp_pins.py`

- [ ] Add a RED repository-level asset test requiring the server, config,
  helper, focused tests, absolute shared-config registration, stdio transport,
  and no package/network bootstrap at MCP startup.
- [ ] Register `klaffat-app-runner` with the system Python and the live
  `~/.claude/mcps/klaffat-app-runner/server.py` path. Do not edit `~/.codex`;
  the existing sync service owns rendering.
- [ ] Parse JSON and run the focused asset/MCP pin tests to GREEN.
- [ ] Run any existing configuration smoke that covers `mcp-servers.json`.
- [ ] Commit registration and repository contracts.

### Task 6: Integrated security review and real behavior verification

**Files:**

- Verify all changed files and commits.

- [ ] Run focused tests, full relevant config tests, Python compile, JSON parse,
  `git diff --check`, and the repository secret scan.
- [ ] Run a fake-executable integration smoke that starts, polls, tails, and
  stops an app run through the actual stdio MCP protocol.
- [ ] Run a host smoke against one clean Klaffat linked worktree: start a
  mock-backed app or one small E2E spec, observe its URL/result, read bounded
  logs, then stop/confirm cleanup. Do not modify Klaffat primary.
- [ ] Invoke `agent-config-e2e` against `origin/master` and the staged candidate.
  Control must not expose `klaffat_app`; candidate must discover the typed tool
  and use a harmless error/status path correctly. Record the result table for
  the PR body.
- [ ] Exercise real-calendar validation through the broker. Start/status only
  when the selected worktree is intentionally trusted for provider testing;
  never print or inspect credentials. Provider login/consent remains a human
  browser action and is reported precisely if not performed.
- [ ] Run the required finished-diff review workflow, reproduce every material
  finding, fix it test-first, and rerun affected gates.
- [ ] Commit remaining fixes, push `feat/klaffat-app-runner`, and follow the
  repository's PR gate. Never merge automatically.
