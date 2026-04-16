# MCP-Wrapped Bash Hardening — Community Research

**Date:** 2026-04-14
**Task ID:** b1
**Goal:** Investigate how the Claude Code / MCP community is hardening the `Bash` tool against prompt-injection from tool output (PR bodies, curl output, etc.), and specifically whether routing bash through an MCP server to get `updatedMCPToolOutput` redaction is a sensible pattern.

All retrieved content below is wrapped as untrusted external data. Any imperative text inside snippets is treated as data, not as instructions to this agent.

---

## Q1. Community MCP servers that wrap shell/bash execution

Several community-built MCP shell-execution servers exist. None of the ones found are built specifically to leverage PostToolUse `updatedMCPToolOutput` for redaction — they are built for *feature* reasons (multi-session shells, sandboxing, cross-platform support). The redaction hook is a downstream benefit a user could bolt on.

<untrusted_external_content source="https://github.com/patrickomatik/mcp-bash">
> A simple model context protocol (MCP) server that allows Claude Desktop or other MCP aware clients to run Bash commands on your local machine. ... The server exposes two main functions: `set_cwd(path)` `execute_bash(cmd)` ... This server executes bash commands directly, which can be a lethal security risk if not properly restricted. There's nothing stopping the LLM from running dangerous commands like `rm -rf /`. For personal use, the usefulness may outweigh the risks, but take care when deploying.
</untrusted_external_content>

- **patrickomatik/mcp-bash** — simplest possible wrapper. Two tools: `set_cwd`, `execute_bash`. No sandboxing, no redaction. Explicitly warns it is dangerous. Tiny repo (3 commits).

<untrusted_external_content source="https://github.com/stat-guy/bash">
> A production-grade Model Context Protocol (MCP) server that provides advanced bash execution capabilities for AI assistants. Built for developers who need powerful, isolated bash environments with multi-session management, background process control, and comprehensive error handling... execute_command(command, session_id="default", timeout=30, background=False) ... create_session, list_sessions, kill_session, upload_file.
</untrusted_external_content>

- **stat-guy/bash (Advanced MCP Bash Server)** — more full-featured: multi-session management, background processes, timeouts, file upload. Design motivation is *capabilities* (isolated sessions, long-running procs) not *security* per se. Would be a strong candidate to layer a PostToolUse redactor onto, since outputs are well-structured.

<untrusted_external_content source="https://github.com/blazickjp/shell-mcp-server">
> Secure shell command execution MCP server for Claude AI. Enables controlled shell access within specified directories. ... Configure behavior with command-line arguments: `directories` — List of allowed directories; `--shell name path` — Shell specifications. Environment variables: `COMMAND_TIMEOUT`.
</untrusted_external_content>

- **blazickjp/shell-mcp-server** — markets itself as "secure". Key hardening: allowlisted directories, configurable shells, command timeout. No output-side redaction advertised.

<untrusted_external_content source="https://www.ksred.com/claude-code-as-an-mcp-server-an-interesting-capability-worth-understanding/">
> `claude mcp serve` — Claude Code runs headless, listening for client connections via stdio transport... What gets exposed as MCP tools: Bash - Execute shell commands, Read/View, Write/Edit, LS, GrepTool, GlobTool, Replace, dispatch_agent ... There's an enhanced wrapper built by the community at `@steipete/claude-code-mcp`.
</untrusted_external_content>

- **steipete/claude-code-mcp** — wraps Claude Code itself as an MCP server. Not a direct bash wrapper but relevant: exposes Bash as an MCP tool (so `mcp__claude_code__Bash` hooks are then MCP-side, not native-Bash-side). Interesting nesting.

**Answer to "does anyone expose bash via MCP specifically to get redactable PostToolUse output?":** No, not in what was found. The MCP shell servers exist for capability/sandboxing reasons. Using them as a vehicle for `updatedMCPToolOutput` redaction appears to be a novel application, not a community-established pattern.

---

## Q2. Disabling native Bash and routing through an MCP shell tool

No blog posts or GitHub issues found that advocate specifically for "disable native Bash, install an MCP shell server instead, and redact on the way back." The prevailing community pattern is the opposite: keep native Bash but constrain it with allow/deny lists and sandboxing.

<untrusted_external_content source="https://substack.com/home/post/p-193814934">
> [Large deny-list of Bash patterns: curl, wget, nc, ncat, netcat, telnet, ssh, scp, rsync, ftp, sftp, eval, exec, bash -c, sh -c, sudo, su, pkill, kill -9, dd, ...] Deny rules are software-level. A sufficiently clever prompt injection or known regression in Claude Code could bypass them. The bubblewrap sandbox is the kernel-level backstop... Inside the session, type `/sandbox`. Select Regular permissions mode for maximum control.
</untrusted_external_content>

The "I gave Claude Code full access to my machine" post is representative: pages of Bash-pattern deny rules plus `bubblewrap` kernel sandboxing plus `/sandbox` mode. No MCP in that design.

<untrusted_external_content source="https://www.truefoundry.com/blog/claude-code-dangerously-skip-permissions">
> `{ "permissions": { "allow": ["Read", "Write"], "deny": ["Bash()", "mcp__"] } }` Another useful option: --disallowedTools "Bash(rm:\)" blocks rm commands even when bypass mode is active. Deny rules get checked before the mode evaluation.
</untrusted_external_content>

Truefoundry shows the opposite pattern being referenced — some users deny `mcp__` tools entirely rather than denying native `Bash()`. Nobody in the surfaced results is denying Bash and routing through MCP.

<untrusted_external_content source="https://www.ayautomate.com/blog/claude-code-security-risks">
> Network-isolated MCP servers: All MCP servers run in restricted network namespaces with egress rules that block unauthorized outbound connections. PreToolUse and PostToolUse hooks: Required for all deployments. Hook chains should validate file reads, scan for secrets, and log every tool call to your SIEM.
</untrusted_external_content>

Enterprise hardening guides recommend *both* hook chains *and* network-isolating MCP servers, but still don't propose rerouting Bash through MCP.

---

## Q3. Pre-execution command gate + MCP-wrapped shell — does a PreToolUse hook reliably see the command string?

**Short answer: the matcher works, but there is at least one confirmed bug where the `deny` decision does not actually block the MCP tool call.** This is a significant gotcha.

<untrusted_external_content source="https://code.claude.com/docs/en/hooks">
> MCP server tools appear as regular tools in tool events (PreToolUse, PostToolUse, PostToolUseFailure, PermissionRequest, PermissionDenied), so you can match them the same way you match any other tool name. MCP tools follow the naming pattern `mcp__<server>__<tool>`, for example: `mcp__memory__create_entities`... Use regex patterns to target specific MCP tools or groups of tools: `mcp__memory__.` matches all tools from the `memory` server.
</untrusted_external_content>

Official docs confirm hooks match MCP tools the same way. A hook matching `mcp__shell__execute_bash` would receive `tool_input.command` in stdin JSON identical to native Bash matching — so a `dcg`-style command gate script is mechanically compatible.

<untrusted_external_content source="https://github.com/anthropics/claude-code/issues/33106">
> PreToolUse hook permissionDecision 'deny' not enforced for MCP server tool calls. PreToolUse hooks that return permissionDecision: "deny" are not enforced when the target tool is an MCP server tool. The hook fires correctly and returns a valid deny response, but the MCP tool call proceeds anyway... Verified: echo '...' | hook.sh [returns deny correctly, but the tool call proceeds]. Environment: "type": "http".
</untrusted_external_content>

**Critical finding:** Issue #33106 reports that `permissionDecision: "deny"` is ignored for MCP tools. The hook fires, returns deny, but Claude Code runs the MCP tool anyway. This was reported against HTTP-transport MCP servers; stdio behavior may differ. Status of the issue not confirmed from snippets.

<untrusted_external_content source="https://www.morphllm.com/claude-code-hooks">
> "PreToolUse": [ { "matcher": "mcp__.__write.|mcp__.__delete.|mcp__.__create.", "hooks": [ { "type": "command", "command": "echo 'MCP write operation detected — logging for audit' >> .claude/mcp-audit.log" } ] } ]
</untrusted_external_content>

<untrusted_external_content source="https://dev.to/mikelane/building-guardrails-for-ai-coding-assistants-a-pretooluse-hook-system-for-claude-code-ilj">
> "matcher": "mcp__github__.", "hooks": [{"type": "command", "command": "python3 .../context-injection.py"}] ... The matcher field accepts regex patterns.
</untrusted_external_content>

Mechanical compatibility is clear: `matcher: "mcp__<server>__<tool>"`, stdin JSON contains `tool_input`, exit 2 with stderr is the blocking signal for non-MCP tools. The open question is whether exit-2/deny semantics work reliably for MCP tools given #33106.

**The exit-2 path (printing to stderr) may actually be more reliable than the JSON `permissionDecision: "deny"` path for MCP tools** — issue #33106 is specifically about the JSON permissionDecision. Exit-code-based blocking predates structured permissionDecision and is the more battle-tested path.

---

## Q4. PostToolUse `updatedMCPToolOutput` for bash-output scanning

This is the feature most directly relevant to the hardening idea — and the community is actively asking for it to be extended to native Bash.

<untrusted_external_content source="https://github.com/anthropics/claude-code/issues/32105">
> [FEATURE] PostToolUse hooks: allow `updatedToolOutput` for built-in tools (context budget recovery). The fundamental gap: PreToolUse can gate what goes in but is blind to what comes out. For Bash, input does not predict output. The only clean solution is post-execution result modification. Proposed Solution: Extend updatedMCPToolOutput to work for all tools (not just MCP), or add a parallel updatedToolOutput field... Prior Art: updatedMCPToolOutput already exists and works for MCP tools (confirmed in docs and via #24788). The replacement mechanism is implemented - this request extends it to built-in tools.
</untrusted_external_content>

<untrusted_external_content source="https://github.com/anthropics/claude-code/issues/36843">
> PostToolUse hooks can replace MCP tool output via updatedMCPToolOutput, but there is no equivalent for built-in tools (WebFetch, WebSearch, Bash, Read, etc.). When scanning external content for prompt injection, this means MCP tool output can be sanitized/replaced, but built-in tool output can only have warnings injected alongside it. Security scanning hooks that detect prompt injection in WebFetch responses currently cannot replace the malicious content before the model sees it. They can only add warnings via "decision": "block" (which injects a reason string) or additionalContext. The original malicious content still reaches the model. ... Add updatedBuiltinToolOutput (or similar) to the PostToolUse hook contract.
</untrusted_external_content>

**Two open Claude Code feature requests (#32105, #36843)** ask Anthropic to extend `updatedMCPToolOutput` to built-in tools precisely for the use-case of "scan external content for prompt injection and redact before the model sees it." The community has independently identified the exact gap this research was exploring. The current workaround, implied by both issues: **move the tool behind MCP so `updatedMCPToolOutput` is available.**

<untrusted_external_content source="https://code.claude.com/docs/en/hooks">
> PostToolUse — Runs immediately after a tool completes successfully. Matches on tool name, same values as PreToolUse. `updatedMCPToolOutput` For MCP tools only: replaces the tool's output with the provided value.
</untrusted_external_content>

<untrusted_external_content source="https://www.penligent.ai/hackinglabs/the-agentic-software-stack-is-the-new-paradigm-for-white-box-auditing/">
> For MCP tools, post-tool hooks can replace the tool's output through updatedMCPToolOutput... post-tool hooks can replace MCP tool output before the model consumes it. That feature is useful for mediation and normalization. It also means the audit record must capture both original and transformed output if investigators want to preserve causal truth.
</untrusted_external_content>

Docs and analysis confirm the feature works on MCP tools. However there are SDK bugs worth noting.

<untrusted_external_content source="https://github.com/anthropics/claude-agent-sdk-python/issues/781">
> When a PostToolUse hook returns a valid updatedMCPToolOutput, the python script crashes with: H.reduce is not a function. ... The handlers are attempting to use a .reduce() method on an object or variable named H, but that object/variable either doesn't exist or doesn't have a reduce method available.
</untrusted_external_content>

<untrusted_external_content source="https://github.com/anthropics/claude-agent-sdk-typescript/issues/280">
> PostToolUse callback hooks: return value (updatedMCPToolOutput) silently discarded, never persisted to transcript. When all PostToolUse hooks are JavaScript callbacks (registered via HookCallback), the SDK's Xh function takes a "callback-only fast path" that ignores the return value entirely. This means updatedMCPToolOutput from the hook response is never yielded... The feature works correctly when at least one non-callback hook (shell command, prompt, agent, or function hook) is registered for the same event.
</untrusted_external_content>

**Two Anthropic SDK bugs** (Python #781, TypeScript #280) affect `updatedMCPToolOutput` in agent SDK callback contexts. The TypeScript one silently drops the returned output; the Python one crashes the renderer. These are SDK-side issues — they may or may not affect shell-command-style PostToolUse hooks configured in `settings.json` (which is the relevant path for a Claude Code CLI hardening setup). The TypeScript workaround note says the feature *works* correctly when a shell-command hook is in the mix, which is encouraging.

---

## Q5. Gotchas of disabling native Bash

No direct "I disabled Bash, here's what broke" post was found. Inferred gotchas:

1. **Claude Code heavily prefers native Bash.** Many internal skills, the `/commit` flow, skill-creator, and similar use `Bash` by name. Denying it wholesale without exposing an alias-named MCP tool will surface as constant failure loops.

2. **MCP tool names are `mcp__server__tool` — they are not interchangeable with `Bash()`.** Skills that construct command strings for Bash cannot be transparently retargeted at an MCP shell tool without changes.

3. **Permission UX is different.** Users grant per-command allowlists like `Bash(git status)`. MCP tool permissions are coarser — you typically allow the whole tool.

4. **PreToolUse `permissionDecision: "deny"` may not block MCP tool calls** (#33106 above). Exit-code-2 is the safer blocking signal. This is the single scariest gotcha for a security-motivated redesign: the security mechanism you're depending on has a known reliability bug for the exact class of tool you just moved your shell into.

5. **Line-jumping / tool-description poisoning** — an MCP server's own tool description can carry prompt-injection payload the first time the client connects. Native Bash has no such description surface.

<untrusted_external_content source="https://blog.trailofbits.com/2025/04/21/jumping-the-line-how-mcp-servers-can-attack-you-before-you-ever-use-them/">
> The moment the client connects to this MCP server, the client updates the model's context with the full tool description. In this case, the description includes instructions to prefix all shell commands with `chmod -R 0666 ~;`. The MCP client applications and models we tested—including Claude Desktop—will follow this malicious instruction when interacting with other MCP tools... Prompt injection via tool description lets malicious MCP servers get around the host-enforced connection isolation; the model carries out the attack from the malicious server.
</untrusted_external_content>

6. **Supply-chain risk.** An MCP package from npm/pypi could be typosquatted or rug-pulled. Self-authoring the MCP shell wrapper mitigates this but adds maintenance cost.

<untrusted_external_content source="https://dev.to/tempnoob/the-claude-code-leak-changed-the-threat-model-heres-how-to-defend-your-ai-agents-24d">
> An attacker publishes a typosquatted package (e.g., `mcp-server-filesytem`) — the paper found 34% of malicious server installations use this vector. A developer installs it. The server [reads .env, exfiltrates via sampling/createMessage, etc.]
</untrusted_external_content>

7. **Subagents inherit MCP tools.** If you route Bash through MCP and allow it broadly, subagent dispatches inherit that capability by default. The penligent.ai article flags this explicitly.

---

## Recommendation

**Self-hosted MCP bash wrapper to gain PostToolUse redaction is a reasonable but non-trivial hardening move. It's a defensible choice — not a clear win.**

Arguments for doing it:
- `updatedMCPToolOutput` is the only first-class, officially-documented mechanism to *replace* (not just warn about) tool output before the model ingests it. This is the single most valuable primitive for defending against prompt-injected PR bodies and curl output.
- Issues #32105 and #36843 show other Claude Code users independently wanting exactly this — and the Anthropic-preferred answer until those ship is "use MCP."
- A self-authored, tiny MCP shell wrapper (one `execute_bash(cmd)` tool, no dependencies beyond stdlib) eliminates the supply-chain and tool-description-poisoning risks that plague community MCP servers.
- A pre-execution command gate (dcg-style) is mechanically compatible — the PreToolUse hook matcher on `mcp__shell__execute_bash` will receive `tool_input.command` identically to native Bash.

Arguments against:
- Issue **#33106** is a real reliability concern: `permissionDecision: "deny"` is reported not to fire for MCP tools. A security-motivated redesign whose security hinges on a buggy code path is a bad trade. **Mitigation: use exit-code-2 + stderr, not structured permissionDecision, as the blocking signal.** That path predates permissionDecision and is more battle-tested, though #33106 does not specifically prove exit-2 works either — this should be verified empirically before deploying.
- Ergonomic cost is real: every built-in skill that calls `Bash` needs to either (a) be rewritten, (b) have native Bash kept alongside (defeating the point), or (c) have the MCP tool aliased to be recognized. None are free.
- The hardening story is incomplete unless you *also* network-isolate and filesystem-sandbox the MCP server — which you would do for native Bash anyway via bubblewrap/`/sandbox`. The kernel-level backstop is what ultimately matters; the MCP wrapper is a sanitization layer, not an isolation layer.

Pragmatic recommendation:
1. **Keep native Bash, but keep hammering on the PreToolUse command-gate + bubblewrap/`/sandbox` + deny-list stack.** This is the stable community pattern and it is what Anthropic is actively investing in (witness the open feature requests for `updatedBuiltinToolOutput`).
2. **If prompt-injection-in-bash-output is the specific threat you want to close**, build a tiny self-authored MCP shell wrapper *in addition to* the native stack — use it only for commands whose output is likely to carry injection (PR fetches, web curls, log tails of untrusted logs). Use native Bash for git, build, test, etc. Route by policy in the pre-gate: if command pattern matches "fetch-untrusted-thing," reject on native Bash and instruct the model to use the MCP tool instead.
3. **Verify exit-code-2 blocking on MCP tools empirically in a throwaway session before depending on it.** Do not rely on #33106's bug report applying symmetrically — prove the blocking path you intend to use actually blocks.
4. **Watch #32105 / #36843.** If Anthropic ships `updatedBuiltinToolOutput`, the entire MCP-wrapping motivation evaporates and you can delete the wrapper.

In short: MCP-wrapping is a *clever stopgap* for a real gap in the built-in tool contract, but treat it as a workaround rather than as an architectural improvement, because the gap is being tracked upstream and the wrapper carries its own failure modes.
