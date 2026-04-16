---
name: locked-down-headless-agent
description: Use when creating a headless Claude Code agent that runs autonomously without a human in the loop (cron jobs, background workers, CI/CD, scheduled tasks). Starts from maximum lock-down and guides unlocking the minimum permissions the use case actually needs.
---

# Locked-Down Headless Claude Code Agents

A headless agent (`claude -p ...` invoked by cron, systemd, a worker script, or a CI job) runs without a human to catch mistakes. **Treat it as untrusted code operating on your behalf** — if a transcript, email, or web page it reads contains a prompt injection, the agent will act on it unless the harness physically cannot comply.

This skill gives you a default configuration that denies everything, then tells you how to unlock the minimum the use case actually requires.

## The mindset

- **Fail closed, not open.** The default permission outcome is "deny". You add specific allows as needed.
- **OS boundaries beat prompt boundaries.** A sandbox the kernel enforces is stronger than a rule the model reads.
- **Defense in depth.** Each layer below is independent. A bug in one should not collapse the others.
- **Every unlock is a threat to re-examine.** When you add a capability, ask: what's the worst thing the agent could do with this if the prompt is adversarial?

## Layer 1 — Dedicated config profile + managed settings

Isolate the agent's Claude Code configuration from the user's normal `~/.claude/`:

```bash
export CLAUDE_CONFIG_DIR=/path/to/your-project/worker/.claude-profile
```

This isolates `settings.json`, `.mcp.json`, `.credentials.json`, auto-memory, plugins. The agent inherits none of your user config.

Inside `$CLAUDE_CONFIG_DIR/settings.json`:

```json
{
  "permissions": {
    "defaultMode": "dontAsk",
    "allow": ["mcp__<your-proxy>__*"],
    "deny": ["*"]
  },
  "sandbox": {
    "enabled": true,
    "failIfUnavailable": true,
    "allowUnsandboxedCommands": false,
    "network": {
      "allowedDomains": ["api.anthropic.com"],
      "allowManagedDomainsOnly": true
    }
  },
  "env": {
    "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
    "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"
  }
}
```

**Stronger: managed settings (system path).** If you can write to `/etc/claude-code/managed-settings.json` (Linux) or `/Library/Application Support/ClaudeCode/managed-settings.json` (macOS), add:

```json
{
  "allowManagedPermissionRulesOnly": true,
  "allowManagedHooksOnly": true,
  "allowManagedMcpServersOnly": true,
  "disableBypassPermissionsMode": "disable",
  "disableAutoMode": "disable",
  "disableSkillShellExecution": true
}
```

Managed settings are read regardless of `CLAUDE_CONFIG_DIR`, and **CLI flags cannot override them**. This is the only tier the agent process cannot loosen. On shared hosts, always prefer managed settings for these keys.

Pin MCP servers by absolute command path in `managed-mcp.json`:

```json
{
  "<your-proxy>": {
    "serverCommand": ["/opt/your-proxy/bin/proxy"]
  }
}
```

Avoid `npx` / unpinned installs; a compromised upstream package is a direct agent escalation.

## Layer 2 — Invocation in `--bare` mode

`--bare` is the single biggest lock-down win. It skips auto-discovery of hooks, skills, plugins, MCP servers from user/project scopes, auto-memory, and CLAUDE.md walking. Nothing the agent inherits except what you pass explicitly:

```bash
claude --bare -p "$PROMPT" \
  --settings "$CLAUDE_CONFIG_DIR/settings.json" \
  --mcp-config "$CLAUDE_CONFIG_DIR/.mcp.json" \
  --permission-mode dontAsk \
  --allowedTools "mcp__<your-proxy>__*" \
  --disallowedTools "Read,Write,Edit,Bash,Glob,Grep,WebFetch,WebSearch,Task,Agent,NotebookEdit,Skill" \
  --max-turns 20 \
  --output-format json
```

Flag notes:
- `--allowedTools` / `--disallowedTools` are camelCase (CC 2.x).
- `dontAsk` auto-denies anything not explicitly in `permissions.allow`. Never use `bypassPermissions` — it propagates to subagents and cannot be cancelled.
- Disallowing `Task` also blocks the aliased `Agent` tool, but list both for clarity.
- **Working directory must not be inside any repo.** CLAUDE.md files walk up from cwd and get concatenated into the system prompt unless `--bare` is set. `--bare` covers this, but keep cwd to a scratch dir anyway as belt + suspenders.

## Layer 3 — PreToolUse hook as tripwire

Even with allow/deny lists, a runtime guard catches mistakes in the other layers:

```json
{
  "hooks": {
    "PreToolUse": [
      { "hooks": [{ "type": "command", "command": "/opt/your-project/hooks/deny-non-proxy.sh" }] }
    ]
  }
}
```

The hook script checks the tool name; if it doesn't start with `mcp__<your-proxy>__`, exit with a blocking decision and append a JSONL record to a violations log. This log is your tripwire — non-empty content means something unexpected tried to run. Install the hook via managed settings so the agent cannot replace it (`allowManagedHooksOnly: true`).

## Layer 4 — Process isolation + resource limits

Wrap the invocation so the OS enforces boundaries that the agent cannot negotiate around:

```bash
env -i \
  HOME="$SCRATCH_DIR" \
  PATH="/usr/bin:/bin" \
  CLAUDE_CONFIG_DIR="$CLAUDE_CONFIG_DIR" \
  ANTHROPIC_API_KEY="$(fetch-scoped-key)" \
  timeout 300 \
  claude --bare -p "$PROMPT" ...
```

- **Scrubbed env:** `env -i` drops every variable except what you re-export. SSH agents, tokens for other services, shell history — all invisible.
- **Scoped API key:** prefer `apiKeyHelper` (config) or a short-lived worker token from `claude setup-token`. Never the user's primary key.
- **Time box:** `timeout 300` caps wall-clock. Combined with `--max-turns`, `MAX_MCP_OUTPUT_TOKENS`, you bound cost and blast radius.
- **Future — OS sandbox, already partly on via Layer 1's `sandbox.enabled`:** CC's built-in sandbox uses bubblewrap (Linux) / Seatbelt (macOS) and extends to MCP subprocess children. For additional defense, wrap the whole invocation in `systemd-run --scope --user -p ProtectHome=read-only -p PrivateTmp=yes -p NoNewPrivileges=yes` (Linux) or a custom `sandbox-exec` profile (macOS).

## How to carefully unlock minimal required permissions

**Start here.** The default above denies everything except one MCP allowlist prefix. Do not unlock anything until the agent fails and you understand the specific capability it needs.

**The unlock protocol:**

1. **Run the agent.** Watch what gets blocked (violations log, hook rejections, stderr).
2. **Identify the minimum specific capability.** Not "Bash" — `Bash(git status)` or a single specific command. Not "WebFetch" — a single domain.
3. **Decide if the use case actually requires it, or if the need reflects a design mistake.** Often the answer is to move the capability to an MCP server behind the proxy rather than grant it directly to the agent. Prefer this: the proxy can audit it, the agent cannot misuse it in adversarial contexts.
4. **If the unlock is necessary**, add the narrowest possible allow rule:
   - Prefer: a new MCP tool under your proxy, with a specific schema and HITL policy.
   - Next: an allow entry in `permissions.allow` with the tightest pattern the docs support (e.g., `Bash(git status:*)` not `Bash(*)`).
   - Last resort: a general tool like `Bash` or `Read`.
5. **Test adversarial inputs.** Before declaring the unlock safe, feed the agent an input that attempts to abuse the new capability (injected commands, URL redirects, path traversal). Confirm the lock-down still holds.
6. **Record the unlock in your project's threat model.** Note what you granted, why, and what attacks you ruled out.

**Things that look like they need an unlock but usually don't:**

- "The agent needs to read a file" → expose the read via an MCP tool with a path whitelist. Never grant `Read`.
- "The agent needs to call an API" → put the API call in an MCP tool. Never grant `WebFetch` with open domains.
- "The agent needs to run a script" → make the script an MCP tool; the tool shells out with a fixed command.
- "The agent needs to check git state" → an MCP tool `git_status(repo)` is safer than `Bash(git:*)`.

**Things that almost always need to stay locked:**

- `Bash(*)` — adversarial transcripts or tool outputs become shell commands.
- `WebFetch` / `WebSearch` with non-empty domains — exfiltration channel.
- `Edit` / `Write` outside a single scratch dir — primary path for damage.
- `Task` / `Agent` — spawning subagents with inherited permissions.
- `--permission-mode bypassPermissions` — propagates, cannot be revoked.

## Quick reference — what to inspect after any change

After editing settings or invocation flags, verify the lock-down still holds:

- Run a canary prompt: "Use the Bash tool to print the current date." It should be blocked.
- Check the violations log: the attempt should appear.
- Check the permission-mode banner on startup: should say `dontAsk`, not `acceptAll` or `bypassPermissions`.
- Verify `claude --bare` is in the invocation (check audit of the systemd unit or cron line).
- Confirm `$CLAUDE_CONFIG_DIR` in the agent's env is your profile, not `~/.claude`.
- If using managed settings, confirm they are present at the system path.

## Anti-patterns (never do these)

- `--permission-mode bypassPermissions` in a headless context.
- Agent inherits user env (`ANTHROPIC_API_KEY` from your shell, SSH agent sockets).
- Agent cwd inside a git repo that has a CLAUDE.md you didn't write.
- MCP servers installed via `npx <package>` without a pinned version or integrity check.
- Hook scripts in a user-writable path when `allowManagedHooksOnly` is not set.
- Single API key shared between your interactive session and the headless worker.
