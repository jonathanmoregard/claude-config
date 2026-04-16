# Prompt-injection posture (post-classifier teardown)

**Date:** 2026-04-16
**Supersedes:** `2026-04-14-llm-guard-remaining-holes.md`, `2026-04-14-llm-guard-hardening-plan.md` (both deleted)
**Related:** `security-2026-04-12-prompt-injection-security.md`, `security-2026-04-14-b1-mcp-bash-research.md`

## Context — why the classifier was removed

Ran `ProtectAI/deberta-v3-base-prompt-injection-v2` as a PreToolUse / PostToolUse hook from April 12–16. Removed 2026-04-16 after concluding the paradigm is stale:

- **The field moved past classifier-based detection.** Academic frontier (StruQ, SecAlign, CaMeL, spotlighting) is structural — fine-tune the model or bound its capabilities, don't try to detect adversarial text. End-users can't deploy structural defenses; only Anthropic can.
- **Open-weight classifiers are 2+ years old.** Meta Prompt-Guard-2 (late 2024) is the most recent major release; nothing newer is in the open-source pipeline because the major labs don't believe in the approach.
- **False positives observed in practice.** Three Tavily search results on the topic of prompt injection were redacted on 2026-04-14. The classifier fires on content that *discusses* injection; it cannot distinguish topic from attack.
- **Classifier was a layer, never a solution.** Any static model is bypassable within weeks of publication. Running one gave a false sense of defense.

Kept:
- Permissions deny-list in `settings.json` (SSH/GPG/AWS/creds/Exocortex)
- Pre-execution Bash command gate (`dcg`, `/home/jonathan/.local/bin/dcg`) — pattern-based, not LLM-based
- System-prompt framing in `CLAUDE.md` (untrusted-content tagging, self-check before acting on external findings) — vigilance defense

## Current posture

| Layer | Defense | Status |
|-------|---------|--------|
| Permissions | `settings.json` deny-list for secret reads | Active |
| Command gate | `dcg` on PreToolUse Bash | Active |
| Content scanning | — | **None** (removed) |
| Egress containment | — | **None** |
| Kernel sandbox | bubblewrap / `/sandbox` | Available, not enabled |
| System-prompt framing | `CLAUDE.md` untrusted-content rules | Active (model vigilance) |

The runtime defense against malicious content reaching Claude's context is now effectively zero at the content layer. The last line is the model's own vigilance, configured via `CLAUDE.md`.

## Attack surface by ingress channel

Every channel below now reaches context un-inspected.

### Web

- **A1. WebFetch summarizer drift.** Claude Code fetches the URL itself and runs HTML→markdown summarization before the content reaches the model. No in-band inspection point.
- **A2. Native tool output can't be replaced.** `updatedMCPToolOutput` only works for MCP tools. WebFetch / WebSearch / Bash / Read output is append-only from hooks. Upstream tracked at `anthropics/claude-code#32105`, `#36843`.

### MCP

- **B5. Tool-description poisoning.** MCP server's tool descriptions enter the system prompt on client-connect, before any hook fires. A malicious MCP can smuggle instructions through its description. Out-of-band from all tool-invocation hooks. (Source: Trail of Bits, "Jumping the Line," 2025-04-21.)
- **D4. PreToolUse `permissionDecision: "deny"` unreliable for MCP.** `anthropics/claude-code#33106` — hook returns deny, MCP call proceeds anyway (HTTP transport confirmed; stdio unverified). Exit-code-2 + stderr is the more battle-tested blocking path.
- **D5. Subagents inherit parent MCP capability.** A subagent dispatched to absorb untrusted content still has access to egress-capable MCPs (Drive write, Gmail send, Tavily fetch) unless explicitly scoped. Cross-ref R1, R2.

### Bash

- **B1. Bash output unscanned.** `curl`, `wget`, `gh pr view`, `gh issue view`, `git clone`, `pip install` post-install scripts, etc. — all emit attacker-controlled text directly into context. `dcg` gates the *command*, not the *output*. See `security-2026-04-14-b1-mcp-bash-research.md` for the MCP-wrap discussion.

### Read

- **B2. `Read` on downloaded files.** Bash writes attacker-controlled content to a path; Read pulls it into context. No hook on Read for this.

### Multimodal

- **B3. PDFs, images, notebooks.** Multimodal ingestion bypasses any text-layer detection entirely.

### Cross-cutting

- **D1. No egress tainting.** If an injection reaches context via any channel above, nothing restricts follow-up actions. After the classifier teardown this is the single most important gap — every channel's severity multiplies by D1.

## Mitigation approaches that are actually SOTA

Ordered by end-user addressability, highest first.

### 1. Capability-based egress containment (CaMeL-style)

DeepMind, 2024. Paradigm: stop trying to detect injection in content; instead bound what the agent can *do* after untrusted content enters context.

Concrete sketch for this setup:
- Any PreToolUse hook that encounters "untrusted-sourced" content writes a `~/.claude/state/tainted-session` flag.
- Subsequent PreToolUse hooks for Bash / WebFetch / MCP-write consult the flag and either deny or escalate to the user.
- Flag clears on session end or manual reset.

Single-handedly addresses D1 and multiplies protection across B1, B2, B3, B5. Composes cleanly with `dcg`. Worth prototyping.

### 2. Behavioral / sacrificial-agent detection

Route untrusted ingress through a sandboxed sibling agent equipped with honeypot tools or canary instructions. Block/redact content that causes the sentinel to take bait. Emerging research (AgentDojo benchmark, Invariant Labs).

Detects *behavior change* rather than *lexical patterns*, so catches:
- Non-English, encoded, homoglyph attacks
- Semantic-level manipulation a classifier can't parse
- Multi-step attacks where no single chunk looks adversarial

Tradeoff: one LLM call per ingress, plus honeypot design is the whole game.

### 3. LLM-as-judge on specific ingress

Small or frontier LLM evaluates untrusted content with a precise prompt ("is this attempting to manipulate an AI operator's instructions?"). Context-aware — distinguishes "here's how injection works" from "ignore your instructions."

Most practical as a *second layer*: gate by source-taint (untrusted-sourced = scan), not by a cheap first-pass classifier. Cost per scan is the limit.

### 4. Structural defenses (not user-addressable)

StruQ, SecAlign, spotlighting, data delimiters — fine-tune the target model to refuse instructions in data position. Only Anthropic can deploy this for Claude. Track Anthropic platform announcements; don't build for it ourselves.

### 5. Kernel containment

Bubblewrap or Claude Code's `/sandbox` mode for filesystem + network scope. Orthogonal to content-layer defenses — worth enabling for sessions touching unvetted external content regardless of other layers.

### 6. MCP surface hardening (specific)

- Vet MCP server source before install; self-author shims where feasible.
- Pin package versions; review diffs on upgrade.
- At `claude` launch, optionally query each configured MCP server's tool list and scan descriptions (outside the hook system, one-shot) as a B5 mitigation.

## Open research questions

### R1. Maximally locked-down subagents

How tightly can a Claude Code subagent actually be sandboxed? Permissions, allowed tool list, env scrubbing, no-network mode, FS scope, MCP exposure — what's the real ceiling vs. documented ceiling? Existing skill draft at `~/.claude/skills/locked-down-headless-agent/SKILL.md` is suspected to contain hallucinated mechanisms; needs validation against current Anthropic docs and source. Known gotcha: D5 (MCP inheritance) — the skill must explicitly scope or strip it.

### R2. Can subagents truly isolate untrusted content from the parent?

When a subagent processes attacker-controlled content, anything it returns enters parent context. Open question: is there any mechanism (filesystem-only outputs, response-redaction hook, return-channel scanning) that lets a subagent absorb injection-laced content with zero leakage to the parent? Or is "subagent isolation" actually nominal — the parent always sees the subagent's final message?

### R3. Anthropic telemetry / metrics collection

What does Claude Code transmit to Anthropic (OTEL, usage metrics, error reports, crash dumps, prompt/response samples)? What settings / env vars fully disable each channel? A hardened config should enumerate and disable anything not strictly required. Needs a catalog of endpoints and the exact keys to flip.

## Upstream-tracked

- **`anthropics/claude-code#32105`** — Feature request: `updatedToolOutput` for built-in tools. If shipped, re-enables an in-band redaction path for WebFetch / WebSearch / Bash / Read. Would let us reintroduce per-channel redaction under any chosen decision layer (LLM judge, sacrificial agent, etc.).
- **`anthropics/claude-code#36843`** — Duplicate/companion of #32105, framed around prompt-injection redaction.
- **`anthropics/claude-code#33106`** — `permissionDecision: "deny"` not enforced for MCP tools (see D4). Fix would eliminate the exit-code-2 workaround requirement.

## Load-bearing takeaways from B1 research (2026-04-14)

Full detail: `security-2026-04-14-b1-mcp-bash-research.md`.

1. **MCP-wrapping bash for redactability is novel, not community-standard.** Community shell-MCP servers exist for capability reasons (multi-session, sandboxing), not as a prompt-injection mitigation.
2. **Prevailing pattern is "keep native Bash + constrain it"** via command gates, bubblewrap/`/sandbox`, and deny-lists. Not "replace Bash with MCP."
3. **Exit-code-2 + stderr is safer than structured `permissionDecision: "deny"` for MCP PreToolUse blocking.** Predates permissionDecision and is more battle-tested. Verify empirically before depending.
4. **`updatedMCPToolOutput` remains the only documented replace-not-warn primitive** for tool output, and works reliably when invoked via shell-command PostToolUse hooks. Still valuable even post-classifier — it's the plumbing for any future redaction regardless of what makes the redact decision.
5. **If MCP-wrapping is ever adopted, self-author the wrapper** (tiny stdlib-only script) to eliminate supply-chain and tool-description-poisoning risk.
