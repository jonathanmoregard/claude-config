---
name: researching-prior-art
description: Use when starting a new feature, choosing a library, or before committing to a design. Triggers on "How do I [implement/build/create/add] X", "What's the best way to [solve/approach/handle] X", "How should I structure/organize/design X", "Which library/tool/framework should I use for X". Also use when an upstream skill (brainstorming, writing-plans) requests prior-art survey.
---

# Researching Prior Art

## Overview

Pure function. Input: a topic or problem. Output: a survey report with an Adopt/Extend/Compose/Build decision matrix. Return the report to the caller and stop — do not chain to another skill, do not begin implementation, do not write code. The caller decides what happens next.

If invoked standalone on an unscoped question (no concrete topic noun, no clear problem statement), ask one scoping question first before enumerating channels. If invoked while a brainstorming session is active and clarifying questions are not yet complete, defer — return `status:skipped:premature` and let brainstorming call you at the prior-art step.

## Impact Gate (When to Skip)

Two layers.

### Always-skip override (mechanical, no judgment)

Return `status:skipped:no-impact` immediately when:
- Pure refactor inside a single file
- Rename / formatting / lint fix / comment edit
- Version bump or dependency upgrade with no API change
- One-line bug fix in already-chosen library code

### Judgment skip

Return `status:skipped:no-impact` when:
- The question involves no third-party tool, library, or pattern choice
- The answer would not change the design
- The caller explicitly marks the request trivial

Do **not** skip on bare ambiguity ("add an endpoint", "add a CLI flag"). Apply the impact gate to the specifics: framework already chosen + mechanical addition → skip; fresh integration or library swap → run.

## Available Channels

Claude has no runtime "list my tools" API; the tool list is in the system prompt and read once. At start, scan the in-context tool list and build the `channels` list:

1. Scan the tool list visible in your system prompt for tool names or descriptions containing `search`, `fetch`, `research`, `web` (any MCP, plugin, or platform tool).
2. Note built-in web-search and web-fetch tools if listed as available in this session.
3. Local indexes are always available: `rg` over the current repo, package manifests, `~/.claude/settings.json`, `~/.claude/skills/`, `~/.codex/skills/`, plugin skill directories.

Report the enumerated list verbatim in the output's `Channels used` field. List unavailable channels in `Channels skipped` with one-line reasons. Never fabricate findings when a channel is blocked — return `status:blocked:<reason>` and let the caller decide.

## Local-Before-External

Exhaust local channels (channel group 3) before touching external ones (channel groups 1, 2). If local sources answer the question, return without external search.

## External Survey (External Channels Only)

Local channels do not take "anti-pattern" or "license" queries — this section applies only to external sources.

### Enforceable caps (count-based; Claude has no wall clock between calls)

- Max **5 distinct external queries**
- Max **15 sources reviewed** in total
- **Stop early** after two consecutive queries return zero new candidates

For human reviewers of agent logs, ~15 minutes wall-clock is a reasonable rough budget — not operationally enforceable inside the skill.

### Query strategy

The 5 queries should cover:
- Exact-match search
- Related-problem search
- Anti-pattern search ("X considered harmful")
- Comparison search ("X vs Y")
- License / maintenance search

### Source scan filters

Quality signals: active maintenance, recent commits, license clarity, ecosystem fit. Discard stale/abandoned/unlicensed candidates.

### Datapoint floor

At least **2** independent datapoints per recommendation. Below floor → mark recommendation as `low-confidence`.

## Output: Adopt / Extend / Compose / Build Matrix

Primary deliverable. The 4-tuple is borrowed from `affaan-m/everything-claude-code/skills/search-first/SKILL.md` (synthesized by that author; not a canonical analyst framework).

| Decision | Meaning | Components |
|----------|---------|------------|
| **Adopt** | Exact-match library/tool; use as-is | Single source URL |
| **Extend** | Close match; fork or wrap | Single source URL + wrap location |
| **Compose** | Combine multiple existing pieces | List of 2+ source URLs |
| **Build** | Nothing suitable exists | N/A; justify in Cons/Evidence; mark `low-confidence` if absence-of-evidence |

### Output shape

```markdown
## Prior-Art Survey: <topic>

**Status:** done | skipped:no-impact | skipped:premature | blocked:<reason> | no-prior-art-found
**Channels used:** <list> | **Channels skipped:** <list with reason>
**Queries run:** <count> / 5 max  |  **Sources reviewed:** <count> / 15 max
**Datapoints:** <count>

### Local findings
- ...  (or "none — local channels checked: <list>")

### External findings
- ...  (or "none — external channels checked: <list>")

### Decision Matrix
| Option | Decision | Components | Source | License | Pros | Cons | Evidence |
|--------|----------|------------|--------|---------|------|------|----------|
| ...    | Adopt    | ...        | ...    | MIT     | ...  | ...  | ...      |

### Recommendation
<one sentence; "low-confidence" tag if datapoints below floor or status = no-prior-art-found>

### Open questions
- ...
```

### Status values

- `done` — survey ran, matrix populated.
- `skipped:no-impact` — impact gate fired; no research performed.
- `skipped:premature` — invoked while a brainstorming session is mid-clarification; deferred.
- `blocked:<reason>` — channels unreachable; caller decides whether to retry or proceed without prior-art context.
- `no-prior-art-found` — channels worked, returned no usable candidates; matrix shows only `Build` row with low-confidence tag.

### Matrix omission

For non-`done` statuses (`skipped:*`, `blocked:*`, `no-prior-art-found` with no candidates), omit the Decision Matrix section entirely (replace with one-line "n/a — see status"). Local/External findings sections also omit if the corresponding channel set was not used.

## Anti-Patterns

- Rabbit-holing past the query/source caps.
- Fabricating citations when a channel is blocked — escalate instead.
- Silently skipping unavailable channels — list them in the output.
- Endorsing stale, low-star, or unlicensed repos without flagging the risk.
- "Apply X to Y" recommendations without genuine novelty or evidence of fit.
- Naming a specific research tool in the recommendation as if the caller must use it — stay tool-agnostic.

## Pure-Function Contract

<HARD-GATE>
Return the report and stop. Do NOT invoke writing-plans, brainstorming, executing-plans, subagent-driven-development, or any implementation skill. Do NOT begin coding. Do NOT create files outside the report. Do NOT call yourself recursively. The caller decides what happens next.

**Permitted:** Asking the caller a single scoping question before enumerating channels (per Overview) is permitted dialogue — it is not a "next-skill" invocation.
</HARD-GATE>
