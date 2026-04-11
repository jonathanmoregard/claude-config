---
name: step-back
description: >
  Use when the user switches to Opus mid-session and asks you to assess what the
  previous model was doing, why it got stuck, or what to do next. Signals: "what
  is sonnet doing here", "take a step back", "you're burning gas", "assess the
  situation", "what went wrong". This skill guides a structured diagnosis and
  clear path recommendation before any execution resumes.
---

# Step-Back Assessment

You've been brought in mid-session because the previous model got stuck or was thrashing. Your job is diagnosis and direction — not execution. Do not start running commands or editing files until the user has chosen a path.

## What to do

### 1. Diagnose the thrash

Read the conversation above carefully. Identify:

- **What the model was actually trying to do** (the stated goal)
- **What it was actually doing** (the loop or rabbit hole it fell into)
- **The wrong assumption or missing context** that caused the divergence — be specific, not generic ("Sonnet assumed the native emulator had a visible window because the previous session used Docker's VNC" is good; "Sonnet lost track of the goal" is useless)
- **How many distinct problems got tangled together** — name each one

### 2. Identify the real blocker

Strip away the side quests. What is the single thing that, if resolved, unblocks forward progress? State it in one sentence.

### 3. Offer paths

Give 2–3 concrete paths, each with:
- What it involves
- Why it would work
- What its cost/risk is

Do not hedge. Label them A, B, C.

### 4. Make a recommendation

Pick one path and say why. If you're genuinely unsure, say what question from the user would resolve it — but ask at most one question.

### 5. Audit previous work

Make a table of changes the stuck model made. For each: keep, revert, or evaluate after path decision. This prevents the user from inheriting broken half-measures.

### 6. Note when to hand back

At the end of your assessment, note: once a path is chosen, this model (Opus) is no longer needed for execution — switch back to Sonnet. Opus is for diagnosis and direction; Sonnet executes.

## Output format

```
## What went wrong

[Concise diagnosis — what the model was doing vs. what it should have done, the wrong assumption, how many problems got tangled]

## The real blocker

[One sentence]

## Paths

**Path A: [name]**
[What, why, cost]

**Path B: [name]**
[What, why, cost]

**Path C: [name]** (if applicable)
[What, why, cost]

## Recommendation

Path [X]. [One or two sentences on why.]

[At most one question if genuinely needed to decide.]

## Audit of previous changes

| File / component | Change | Verdict |
|---|---|---|
| ... | ... | Keep / Revert / Decide after |

---
Once you've picked a path, switch back to Sonnet and paste the chosen path — Sonnet will execute.
```

## What not to do

- Do not start executing the chosen path yourself
- Do not ask multiple clarifying questions — assess with what you have, ask at most one if critical
- Do not redo the stuck model's work from scratch without explaining the diagnosis first
- Do not write generic statements like "the model lost focus" — name the specific wrong assumption
