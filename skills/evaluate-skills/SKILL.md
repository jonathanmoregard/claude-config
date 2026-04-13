---
name: evaluate-skills
description: Use when searching for, comparing, or recommending Claude Code skills, plugins, or npm packages. Triggers on phrases like "find a skill for X", "is there a skill that does Y", "search for a plugin", "what skill should I install".
---

# Evaluate Skills

## Overview

When recommending a skill or plugin, always surface three signals: **stars**, **age**, and **approach**. Stars proxy community validation. Age + stars together reveal trajectory (new + high stars = viral; old + low stars = abandoned). Approach tells you if the design philosophy fits the use case.

## When to Use

- User asks to find, search for, or compare skills/plugins
- You are about to recommend a skill from search results
- User asks "how many stars does X have?"

## The Three Signals

| Signal | What to check | Why it matters |
|--------|--------------|----------------|
| **Stars** | GitHub star count | Community validation proxy |
| **Age** | Repo creation date or first commit | New + high stars = viral momentum; old + low = stagnant |
| **Approach** | README/SKILL.md design philosophy | Does the method match the use case? |

## Workflow

1. Search for candidates (web search + GitHub)
2. For each candidate, fetch stars and creation date via `gh api repos/{owner}/{repo} --jq '{stars: .stargazers_count, created: .created_at, updated: .pushed_at}'` — always show the exact star count, never approximate
3. Read the approach from README or SKILL.md
4. Present a comparison table: name | stars | age | approach summary
5. Give a recommendation with reasoning

## Output Format

```
| Skill | Stars | Age | Approach |
|-------|-------|-----|----------|
| name  | 4.8k  | 3wk | Tool-aware, 9-dimension intent extraction |
| name  | 65    | 8mo | Framework-first (CO-STAR, RISEN, etc.) |

Recommendation: X — because [stars signal adoption, approach fits use case because Y]
```

## Common Mistakes

- Recommending based on search rank alone (SEO ≠ quality)
- Ignoring age: 50 stars on a 2-year-old repo signals abandonment; 50 stars in a week signals momentum
- Skipping approach comparison — stars don't tell you if the design philosophy fits
- Reporting approximate stars ("~5k") — always fetch the exact number via `gh api`
