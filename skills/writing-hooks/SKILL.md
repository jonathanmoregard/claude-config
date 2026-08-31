---
name: writing-hooks
description: Use when creating or editing a Claude Code hook (PreToolUse, PostToolUse, PermissionRequest, UserPromptSubmit, Stop, SubagentStop, SessionStart, SessionEnd, PreCompact and the rest), or when a hook interrupts the user's chat instead of notifying quietly, blocks a turn it should not, fires in a loop, never fires, or needs to hand context to Claude. Hooks live under read-only ~/.claude, so the edit belongs in a worktree.
---

# Writing Hooks

Source of truth: https://code.claude.com/docs/en/hooks (31 events; the page
carries no date, latest internal marker v2.1.218). Check it before trusting any
blog — most community references are stale on exit codes and event count.

## Before you write the file

The hook script and its `settings.json` registration both live under
`~/.claude`, which is read-only and pinned to `master`. The harness reads that
working tree live, so a hook dropped there is active — and unreviewed — at once.

```bash
git -C ~/.claude fetch origin master
git -C ~/.claude worktree add ~/worktrees/claude-<slug> -b feat/<slug> origin/master
cd ~/worktrees/claude-<slug>
```

Write `hooks/<name>` and edit `settings.json` **in the worktree**, then commit,
push and `gh pr create`. The merge click is Jonathan's. Never commit to a branch
this session did not create. One-line hook edits included.

## Overview

A hook reads JSON on stdin, prints JSON on stdout, and exits. The field you
print decides whether the message arrives quietly, injects context, or
interrupts the turn. Choose the channel before writing logic.

## Delivery channels

| Goal | Emit | Effect |
|---|---|---|
| Notify the user, don't interrupt | `{"systemMessage": "..."}` | Warning shown to user; turn continues |
| Give Claude context | `{"hookSpecificOutput": {"hookEventName": "<Event>", "additionalContext": "..."}}` | Injected where the event lands |
| Stop the turn ending, no error styling | `hookSpecificOutput.additionalContext` on `Stop`/`SubagentStop` | Renders as "Stop hook feedback"; conversation continues |
| Force a correction with error styling | `{"decision": "block", "reason": "..."}` | Blocks; reason shown as hook feedback |
| Gate a tool call | `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"\|"deny"\|"ask"\|"defer", "permissionDecisionReason": "..."}}` | Fires before permission checks, in every mode |
| Rewrite the tool's arguments | `hookSpecificOutput.updatedInput` on `PreToolUse` (object) | Replaces `tool_input`; rejected if it fails the tool's input schema |
| Rewrite the tool's result | `hookSpecificOutput.updatedToolOutput` on `PostToolUse` | "Replaces the tool output before it is sent to the model". `updatedMCPToolOutput` does the same for MCP tools only — prefer `updatedToolOutput`, which covers all tools |
| Halt Claude entirely | `{"continue": false, "stopReason": "..."}` | Takes precedence over decision fields; `stopReason` shown to user, not Claude |
| Desktop bell / title | `{"terminalSequence": "..."}` | Works even on events that discard `systemMessage` |

`hookSpecificOutput` **requires** `hookEventName` matching the event exactly, or
the output is dropped. Output strings cap at 10,000 characters.

`decision: "block"` is valid on `UserPromptSubmit`, `UserPromptExpansion`,
`PostToolUse`, `PostToolBatch`, `Stop`, `SubagentStop`, `ConfigChange`,
`PreCompact`, `TaskCreated`. `"block"` is its only value — to allow, omit it.
`PreToolUse` uses `permissionDecision`, not top-level `decision` (deprecated
there: `approve`→`allow`, `block`→`deny`).

`suppressOutput` is inert — accepted and ignored.

## Exit codes

- **0** — stdout parsed as JSON only if the first non-whitespace char is `{`.
  Plain-text stdout reaches Claude only on `UserPromptSubmit`,
  `UserPromptExpansion`, `SessionStart`; elsewhere it goes to the debug log.
- **2** — blocking error. JSON is still read (every exit code is), and since
  v2.1.214 exit 2 with schema-invalid JSON still blocks. Exit 2 does nothing on
  `PermissionRequest`, `PermissionDenied`, `Notification`, `StopFailure`,
  `InstructionsLoaded`, `SessionStart`, `SessionEnd`, `SubagentStart`. On
  `PostToolUse`/`PostToolUseFailure` it cannot block but stderr reaches Claude.
- **Other non-zero** — does not block. Valid JSON on stdout decides the outcome
  and the exit code is ignored; otherwise it is a non-blocking error. Exit 127
  from a mistyped path leaves the gate silently disabled. Exit 1 never blocks.
- **Timeout** — hook is cancelled, output discarded, no decision rendered. A
  stalled hook is not a gate. Defaults: 600 s command/http/mcp_tool, 30 s on
  `UserPromptSubmit`, 10 s `MessageDisplay`, 1.5 s shared for `SessionEnd`.

## Input payload

Common: `session_id`, `prompt_id`, `transcript_path`, `cwd`, `permission_mode`,
`effort`, `hook_event_name`, plus `agent_id`/`agent_type` inside subagents.

Event-specific: `tool_name`/`tool_input`/`tool_use_id`, `tool_response`,
`prompt`, `stop_hook_active`, `source`, `trigger`, `last_assistant_message`.

`transcript_path` is written asynchronously and **may lag** the conversation. On
`Stop`/`SubagentStop`, read `last_assistant_message` instead of parsing it.

**Hooks fire inside subagents**, so a `PreToolUse` gate covers agent-dispatched
tool calls too. Measured on 2.1.222 (2026-08-24): a `PreToolUse` deny cancelled a
subagent's `Read` and the reason came back as `is_error` in the subagent's
sidechain transcript; a `PostToolUse` `additionalContext` reached the subagent's
model context verbatim. Note the injected context is **not** persisted in the
transcript — it exists only in the model's context, so grepping the JSONL for it
finds nothing. A hook that must skip subagent traffic filters on `agent_id`.

`permission_mode` reports the UI's "Manual" as `default`, never `manual`.
File-tool paths are always absolute, but arrive backslashed on Windows.

## Guards

- CLI only: `[ "${CLAUDE_CODE_ENTRYPOINT:-cli}" = "cli" ] || exit 0`
- `Stop`/`SubagentStop`: return immediately when `stop_hook_active` is set.
  Claude Code force-overrides after 8 consecutive blocks.
- Unparseable stdin, missing fields, any `OSError` → exit 0 silently.
- Exit non-zero only when a hard block is intended.

## Matchers

Omitted, `""`, or `"*"` matches all. A value of only letters, digits, `_`, `-`,
spaces, `,`, `|` is exact-match or a list; anything else is an **unanchored
regex** — `Edit.*` also matches `NotebookEdit`, so anchor with `^...$`.
Case-sensitive. MCP needs the suffix: `mcp__memory` matches nothing,
`mcp__memory__.*` matches. The per-handler `if` field works only on tool events.

## Configuration

Seven sources (user, project, local, managed policy, plugin, skill, subagent
frontmatter). Hooks **merge** across levels rather than replacing, and all
matching hooks run **in parallel** — one hook's `deny` does not suppress a
sibling's side effects. For `PreToolUse`: `deny` > `defer` > `ask` > `allow`.
Edits are picked up by the file watcher; restart only if it missed them.

## Enforcement

A `PreToolUse` `deny` outranks every permission mode including
`bypassPermissions`. It still is not a hard gate: the `if` filter fails open on
unparseable Bash, timeouts render no decision, and hooks can tighten but never
loosen. Put gates that must hold in MCP server code; use hooks to observe, warn,
and trigger.

Write `additionalContext` as factual statements, not imperative instructions —
prompt-injection defenses may surface imperative text to the user instead.

## Test before registering

```bash
echo '{"session_id":"t","hook_event_name":"Stop","last_assistant_message":"sample"}' \
  | python3 ~/worktrees/claude-<slug>/hooks/my-hook.py; echo "exit=$?"
```

Verify the intended field appears, the exit code is what you meant, and the
no-match case prints nothing. Run it against the copy in your worktree, not the
live one in `~/.claude`; the PR is what puts it there.

## Common mistakes

| Mistake | Fix |
|---|---|
| `decision: "block"` for an advisory nudge | Use `systemMessage`, or `additionalContext` if Claude must act on it |
| Top-level `additionalContext` | Nest it in `hookSpecificOutput` with `hookEventName` |
| `hookEventName` missing or mismatched | Output is silently dropped |
| Blocking on `Stop` without `stop_hook_active` | Loop until the 8-block cap |
| Parsing `transcript_path` on `Stop` | Use `last_assistant_message`; the file lags |
| Non-zero exit on malformed input | Exit 0 silently |
| Unanchored matcher (`Edit.*`) | Anchor it, or it catches siblings |
| Trusting a blog's event list or exit-2 rules | Check the official reference |
