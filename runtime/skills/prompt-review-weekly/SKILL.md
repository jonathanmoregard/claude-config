---
name: prompt-review-weekly
description: Use when the user says "weekly prompt review", "cluster my prompts", "what am I asking for repeatedly", "generate automation backlog from prompts", or similar. Reads the prompt-log JSONL (populated by the UserPromptSubmit hook at ~/.claude/hooks/prompt-log.py) and clusters the last N days of user prompts into topical groups, surfacing repetitive patterns worth turning into skills, hooks, or slash commands.
---

# Weekly prompt review

The UserPromptSubmit hook at `~/.claude/hooks/prompt-log.py` writes one JSONL line per prompt to `~/.claude/automation-review/prompts.jsonl`:

```json
{"ts": "...", "session_id": "...", "cwd": "...", "prompt_len": N, "prompt_head": "..."}
```

This skill turns that raw log into a backlog of "you keep asking Claude to do X — should X become a skill/hook/command?"

Pattern from Zijdel dev-hooks: **the tooling generates its own automation backlog**.

## What to do

1. **Load recent prompts.** Default window: last 14 days. Command:
   ```bash
   python3 -c "
   import json, sys
   from datetime import datetime, timedelta, timezone
   cutoff = datetime.now(timezone.utc) - timedelta(days=14)
   for line in open('/home/jonathan/.claude/automation-review/prompts.jsonl'):
       try:
           e = json.loads(line)
           if datetime.fromisoformat(e['ts']) >= cutoff:
               print(json.dumps(e))
       except Exception:
           pass
   " > /tmp/prompts-window.jsonl
   wc -l /tmp/prompts-window.jsonl
   ```

2. **Cluster.** Read the window and group by topical similarity. Don't lean on embeddings — for a 100-500 prompt window, direct reading + human-like grouping produces higher-signal clusters. Look for:
   - **Verb + object repeats:** "commit and push X", "check CI status", "read the file", "run the tests"
   - **Same tool invoked repeatedly with slight variations:** "gh pr checks 153", "gh pr checks 154", "gh pr view 153" — probably wants a `pr-status` slash command
   - **Same debugging shape across projects:** "why is X hanging", "why does Y timeout", "trace the error in Z"
   - **Setup incantations:** "install X on nixos", "add Y package", "configure Z" — probably wants a skill/module

3. **Filter noise.** Skip:
   - Prompts under 20 chars ("y", "ok", "next", "continue")
   - `/`-prefixed slash commands (already automated)
   - Unique one-off asks (report as "long tail: N one-offs" summary)

4. **Score by leverage.** For each cluster, estimate:
   - **Frequency:** how many times in the window
   - **Fix type:** skill (guidance), hook (mechanized behavior), slash command (aliased prompt), MCP tool (structured API), or "don't automate — genuinely varies each time"
   - **Blocker:** what's stopping the automation today (missing tool, requires human judgment, etc.)

5. **Emit backlog markdown.** Write to `~/.claude/automation-review/backlog-<date>.md`:

   ```markdown
   # Automation backlog — <window start> to <window end>

   Window: <N> prompts across <M> sessions.

   ## Ready to automate (rank by frequency × leverage)

   ### 1. <Pattern name> — N occurrences
   - **Shape:** <verb + object + typical parameters>
   - **Example prompts:** <2-3 quoted>
   - **Recommended:** slash command | skill | hook | MCP tool
   - **Rough impl:** <1-2 sentences>
   - **Blocker (if any):** <what's missing>

   ### 2. …

   ## Investigate further
   <clusters where the pattern is real but the fix isn't obvious>

   ## Long tail
   <count>: N one-off prompts, no repeating pattern.
   ```

6. **Present the top 3-5 recommendations** conversationally so the user can pick which to implement. Do not implement without a nod — automating the wrong pattern wastes effort.

## Anti-patterns

| Mistake | Why bad |
|---|---|
| Automate a low-frequency pattern because "it's elegant" | The point is compounding leverage; ~5 uses/month is the floor |
| Group by tool used (all Bash, all Read) | Meaningless clustering; group by INTENT |
| Treat every "repeat" as an automation candidate | Some tasks genuinely require fresh judgment each time; automating them makes the agent worse |
| Skip filter step (short prompts, slash commands) | Noise dominates the top-N; real patterns disappear |
| Cluster then implement without user pick | You will build the wrong one; user has context the log lacks |

## When to run

- User asks directly ("cluster my prompts", "weekly review")
- Once a week as a habit (add to a Sunday cron if desired)
- After a burst of new activity in an area to see if it's stabilized enough to codify
