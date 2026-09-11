# Codex sync ownership design

## Goal

Keep `~/.codex` derived from Claude and the `ai-client-config` renderer. Agents
must not hand-edit that generated or runtime-owned tree. A refused edit must
name both sanctioned source paths. Separately, restore the hourly private-repo
sync without weakening its existing systemd sandbox.

## Existing system

`ai-client-config-codex-sync.service` clones the private
`jonathanmoregard/ai-client-config` repository, renders from read-only
`~/.claude`, stages every managed output, then publishes selected paths under
`~/.codex`. Codex keeps mutable runtime state beside those generated paths.

The service currently fails at `stage=clone`: its `ProtectHome=tmpfs`
namespace hides GitHub credentials, so Git falls through to an X11 askpass
helper and exits 128. NixOS PR #218 already contains the scoped fix: expose
only GitHub CLI metadata plus the user-bus socket, and use the host-specific
`gh auth git-credential` helper. CI is green; merge remains the required human
gesture.

Claude already runs `path-edit-guard.py` before file-tool writes.
The Codex renderer mirrors that hook and adapts Codex `apply_patch` calls into
Claude-shaped Write/Edit events. Extending this existing guard therefore gives
both clients the same refusal without making Codex runtime storage read-only.

## Chosen design

1. Add a `~/.codex` section to `CLAUDE.md`. It says never to edit that tree
   manually, routes shared behavior/config changes through the `~/.claude`
   worktree/PR flow, routes rendering or Codex-only mapping changes through the
   `ai-client-config` repository, and names the sync service as publisher.
   Because `CLAUDE.md` becomes generated `~/.codex/AGENTS.md`, both clients
   receive the same instruction.
2. Extend the existing file-write guard to deny Write/Edit/MultiEdit/
   NotebookEdit targets anywhere under live `~/.codex`. Keep the current
   `~/.claude` tracked/config-surface policy unchanged. The refusal names both
   sanctioned source routes and explains that Codex itself, not an agent,
   owns runtime state.
3. Update `settings-management` metadata and body so requests to edit
   `~/.codex` trigger the skill and immediately redirect upstream.
4. Add subprocess-level tests first. Cover absolute and tilde Codex paths,
   every file tool, lookalike siblings, traversal out of the root, and the
   actionable refusal text. Existing Claude guard tests remain unchanged.

## Alternatives rejected

- Make all of `~/.codex` filesystem-read-only: breaks session databases,
  history, auth state, caches, logs, and other Codex-owned runtime writes.
- Instruction only: gives the right route but does not stop the common direct
  Write/Edit/apply-patch mistake.
- Duplicate Codex-specific guard: creates drift between clients even though
  the renderer already adapts both onto one hook contract.

## Error handling and boundaries

Malformed hook input remains fail-open under the existing house rule. A valid
file-tool call into `~/.codex` always returns a structured PreToolUse deny. The
guard does not intercept Codex's own internal writes, reads, sync service
writes, worktree edits, or changes in the separate `ai-client-config` repo.

Shell-based writes are governed by the durable agent instruction rather than
new command-string matching. Broad string denial would also block legitimate
inspection and would be less precise than the file-tool boundary requested.

## Verification

- Watch new Codex-target tests fail before implementation.
- Run focused guard tests after each guard edit.
- Run repository config verification after instruction/skill edits.
- Render through `ai-client-config` in an isolated home and assert resulting
  `AGENTS.md` contains the new ownership rule and apply-patch invokes the guard.
- After PR #218 is merged and auto-deployed, start
  `ai-client-config-codex-sync.service`; require success, removal of
  `last-failure`, and a matching `last-success` commit.
