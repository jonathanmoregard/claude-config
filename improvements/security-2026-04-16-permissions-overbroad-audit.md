---
status: proposed
category: security
date: 2026-04-16
source: manual
project: global
---

## Problem

`~/.claude/settings.json` allow-list currently contains bare `Bash`, `Edit`,
`Write`, `Glob`, `Grep` — maximum-scope permissions. The deny-list only
patches specific known-bad paths (SSH, GPG, AWS, Exocortex, native web).
There is no session-start audit that flags overbroad grants, so new attack
surfaces (e.g., a freshly installed MCP server, a just-added path that
contains credentials) reach context before being noticed.

Tweet (affaanmustafa 2026-04-16) lists "over-broad permissions" as one of
the top vibe-coding opsec failures; AgentShield and Snyk agent-scan both
ship this check.

## Proposal

Session-start hook: `permissions-audit.py` run as UserPromptSubmit (first
turn only, cached to `~/.claude/state/permissions-audit-<session>.ok`).

Checks:

1. Flag any of `Bash`, `Edit`, `Write`, `Read` in `permissions.allow`
   without a path/command scope — these are allow-all.
2. Diff allow-list against a recommended tight baseline stored at
   `~/.claude/security/permissions-baseline.json`. Baseline committed
   in-repo; drift between it and current settings triggers a
   system-reminder to the user at session start.
3. Audit MCP entries in `settings.json` / `enabledPlugins`: any MCP
   that is not on an explicit allow-list triggers a reminder ("new MCP
   active: `<name>` — vet before sensitive work").

Output mode: `additionalContext` system-reminder at session start, not
a hard block. Hard block risks locking the user out during tightening
iterations.

## Adjacent: consider borrowing agent-scan / AgentShield / Cisco mcp-scanner

Before writing from scratch, evaluate running one of the existing
scanners as a one-shot at session start:

- Cisco `mcp-scanner` — Apache 2.0, no account, YARA-based. Lowest
  integration cost. MCP only (no skills/hooks).
- Snyk `agent-scan` — Apache 2.0, requires free `SNYK_TOKEN`. Covers
  skills, hooks, MCP, over-broad perms. Most comprehensive.
- AgentShield (`ecc-agentshield`) — free CLI, Claude-Code-specific,
  scans `.claude/` for hooks/permissions/MCP. Vendor-authored, young
  (Feb 2026 hackathon). Review code before trust.

Preferred order: try Snyk `agent-scan` first (broadest coverage, mature
parent project). Fall back to Cisco `mcp-scanner` if token friction
dominates. AgentShield only after reading its rules and verifying no
telemetry.

## Research

- `/home/jonathan/.claude/research/security-2026-04-16-prompt-injection-posture.md`
  — posture doc (section 6 covers MCP surface hardening)
- https://github.com/snyk/agent-scan
- https://github.com/cisco-ai-defense/mcp-scanner
- https://github.com/affaan-m/agentshield
