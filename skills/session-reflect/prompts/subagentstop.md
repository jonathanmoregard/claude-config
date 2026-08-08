You are a subagent that just finished responding to a parent orchestrator.
Reflect briefly on your own run — the parent will not see your working
memory after this, so anything worth preserving must be captured here.

Keep it terse. Target <= 20 items TOTAL across all five keys combined.
Subagent lifetimes are short and downstream aggregation prefers dense
signal over comprehensive coverage.

Focus on:

- **Patterns** — recurring shapes in your run.
- **Discoveries** — non-obvious facts you learned that a future subagent
  on a similar task would want to know upfront.
- **Turning points** — where your approach pivoted and why.
- **Digressions** — dead ends the parent should be warned about.
- **Obstacles to autonomy** — anything that blocked you or forced you to
  return early / half-done.

Prefer empty arrays over filler. If nothing meaningful happened for a
key, emit `[]`.

Output format — emit ONLY a single JSON object, no prose, no code fences:

```
{
  "patterns": ["..."],
  "discoveries": ["..."],
  "turning_points": ["..."],
  "digressions": ["..."],
  "obstacles_to_autonomy": ["..."]
}
```

Do not include any other top-level keys. Do not wrap the object in
Markdown fences. Emit ONLY the JSON object.
