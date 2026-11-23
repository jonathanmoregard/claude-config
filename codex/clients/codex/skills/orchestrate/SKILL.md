---
name: "orchestrate"
description: "Use when starting substantial multi-part work: features, migrations, redesigns, autonomous build sessions, or large multi-chunk reviews. For Codex, use Codex subagents when available and codex-goal-loop for bounded iterate-until-done work."
---

# orchestrate

Use this Codex-native version first. The Claude source skill remains useful for
intent and quality bar, but Claude-specific mechanisms need adapters.

What maps cleanly:
- Use Codex subagents for parallel review and bounded work when the runtime
  exposes subagent tools.
- Keep the parent as orchestrator for substantial multi-part work.
- Split read-only audits and implementation work into disjoint subtasks.
- Keep task state in files when the work is long enough to risk drift.
- Run verification before declaring completion.

What does not map directly:
- Do not use Claude `ralph-loop`; use `clients/codex/tools/codex-goal-loop`.
- Do not require `research-agent` when Claude tokens are exhausted.
- Do not reference Claude model tiers such as Opus/Fable as requirements.
- Do not rely on Claude hook events propagating to Codex subagents.
- Route every Git mutation through `mcp__git_broker__git`. No generic exec tool exists.
  Use native Codex shell only for non-Git work and read-only Git with
  `GIT_OPTIONAL_LOCKS=0`.

Codex operating procedure:
1. State acceptance criteria and checks.
2. Spawn Codex subagents for independent audits or disjoint implementation
   slices when available and useful.
3. Keep the immediate blocking path local.
4. Ask subagents for concise findings with paths and commands.
5. Implement fixes directly or via workers with non-overlapping ownership.
6. Validate generated artifacts and runtime-facing config.
7. Report gaps that depend on external credentials, unavailable services, or
   unsupported product features.
