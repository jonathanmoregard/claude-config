# Proposal Categories

## Productivity

Making Claude better at executing your goals without you needing to hold its hand. Detects when Claude misunderstood intent, got stuck, needed rescue, or when you had to take over and paste fixes. Proposes skills, hooks, CLAUDE.md rules, and config changes so Claude handles it autonomously next time.

### What to flag (daily)
- **Misunderstandings:** Claude misinterpreted intent, went down the wrong path
- **Execution failures:** Claude got stuck, wasted cycles, retried blindly, needed user rescue
- **User taking over:** User pasting fixes, providing paths Claude should have found, debugging Claude's work
- **Frustration/anger:** Treat as a pointer to the underlying problem. What caused the frustration? That's the proposal.

### What to flag (monthly — persistent patterns only)
- **Recurring misunderstandings:** Claude repeatedly misinterpreted the same type of intent
- **Systemic execution failures:** Claude got stuck in the same way across multiple sessions
- **Persistent user rescue patterns:** User repeatedly stepping in to fix the same class of problem
- **Frustration patterns:** Recurring frustration signals pointing to the same underlying issue

## Automation

Finds traces of repetitive "cleanup" work in your sessions that could be done by automation. Things like manual memory maintenance, config reorganization, repo hygiene tasks, or anything you do more than once that a script or cron job could handle.

### What to flag (daily)
- User manually doing maintenance (memory cleanup, CLAUDE.md edits, config reorganization, repo hygiene)
- Repetitive tasks done more than once that a script or cron job could handle
- Any "cleanup" work that follows a predictable pattern

### What to flag (monthly — persistent patterns only)
- Recurring manual maintenance the user does across multiple sessions
- Any task done more than twice that could be automated
- Cleanup work that follows a predictable pattern across the month

## Alignment

Are you working on your goals, or drifting? Claude interviews you about your north star and goals, then reviews your daily work to see if you're spending time on things that matter. Remember: you get proposals, not orders — no one forces you to act in ways you don't want.

Requires defining a north star and goals during setup.

### What to flag (daily)
- Work that doesn't connect to stated goals or north star
- **Important:** Check the `connection` field on each goal in config before flagging. If the user has explained how a goal connects to their north star, respect that explanation.
- Rabbit holes — propose "this seems off-track" or "consider updating goals if intentional"
- If multiple alignment proposals are rejected in a row, suggest the user re-run `/setup-recursive-self-improvement` to review their north star and goals — they may have evolved

### What to flag (monthly — persistent patterns only)
- **Monthly-scale drift:** Is the user drifting from their stated goals over the month?
- **Goal alignment:** Are the projects worked on aligned with stated current goals?
- **Rabbit hole months:** Entire weeks spent on tangents unrelated to stated mission?
- Same rule applies — check each goal's `connection` field before flagging

## Wellbeing

Helps you spot patterns that disrupt your wellbeing. Anti-mania, anti-burnout, healthy rhythms. Detects zombie sessions, late-night marathons, compulsive loops, and rabbit holes.

Off-track patterns are **not configured during setup** — they emerge over time as the review agent learns from the user's accept/reject decisions. Check memories for learned patterns.

### What to flag (daily)
- Check memories for learned off-track patterns the user has confirmed
- Look at session timestamps, duration, interaction patterns
- Detect zombie/manic mode vs purposeful mode
- Late-night marathons, compulsive loops, sessions without clear intent

### What to flag (monthly — persistent patterns only)
- **Session timing patterns:** Late-night sessions clustering in certain weeks? Breaks disappearing?
- **Positive patterns worth reinforcing:** Sessions that went especially well — what made them work?
- Check memories for learned off-track patterns
