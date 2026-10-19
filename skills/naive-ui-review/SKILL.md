---
name: naive-ui-review
description: Use before showing rendered screens (design rounds, screenshots, a built UI) to the founder or users, and whenever screens may read as "built from the inside view" — "naive review", "fresh eyes", "would a new user get this", "theory of mind", "does this make sense to someone who doesn't know the spec", "where am I in the flow", "too much text", "every element should pull its weight", "fewer clicks", or after a founder says they were confused by screens.
---

# Naive UI review

Agents who wrote or read the spec understand every screen. First-time users do not. Measured: LLM walkthroughs completed 97-100% of tasks where first-time humans managed 88%, and found 3 failure points where humans found 10 (Zhong 2025). Asking "what would you do next?" makes a model behave like an ideal navigator. Confusion only shows when you ask for it separately, and only predicts real failures when the reviewer carries its own history from screen to screen.

Two passes, never mixed: a **naive pass** (a reviewer who knows nothing but the screens) and an **expert pass** (checklist, one criterion per call). A run is a list of hypotheses for the founder, not a verdict.

## Inputs

- Rendered PNGs only, in the order a user meets them. One task per walkthrough. Desktop and phone are separate walkthroughs; merge findings after.
- A task in the user's words, not the screen's or the spec's: "A friend sent you a link to pick a date for dinner. Answer it." Never reuse labels, feature names or decision vocabulary from the brief.
- Comprehension questions with ground-truth answers, written by you from the spec and confirmed by the founder once: "After pressing the big button, can guests still answer? (no)". Keep them in the project notes and reuse them every round.

## Withhold from the naive reviewer

Spec, brief, DECISIONS, HANDOFF, COPY, code, file names that leak state (rename PNGs to `step-01.png`, `step-02.png`), the founder's notes, and any earlier review. Dispatch it as a fresh subagent (never a fork, which inherits your context) whose prompt holds only the task, the image paths and the protocol below.

## Naive pass protocol (per walkthrough)

For each screen, in order, the reviewer answers (prompt verbatim, then the screen):

1. **Five seconds:** "Look at the screen as if for five seconds. What is this page for? Where are you in the process? What do you think happens if you press the most prominent button?"
2. **Think aloud:** "Use the think aloud protocol. Be exploratory. Describe what you see, the actions you could take, why each might be right, how confident you are, and the one you would take."
3. **Confusion, rated separately:** "How confusing is this screen for someone in your situation: not, slightly, or very confusing? Why?"
4. **Words:** "List every word, label or sentence you could not confidently explain to a friend."
5. **Memory:** carry the reviewer's own previous answers (screens seen, action chosen, reasoning) into the next screen's prompt. This is its memory, not product knowledge.

After the last screen: the comprehension questions, answered from the screens alone, with "can't tell" allowed.

Runs: 3 per walkthrough, at least two model families when available (Claude plus Codex via `ai-router`). Personas add little; if used, at most two that differ in a way that matters (e.g. phone-only first-timer reading Swedish; the organiser).

## Expert pass (separate calls, one criterion each)

Ask for violations only, with severity 0-4 (no praise). Criteria:

- **Wayfinding:** H1 says where you are or what to do now, and differs per page. The primary button names its outcome (verb + object: "Send invitations", not "Send"). Closing or irreversible actions say so and stand apart. A confirmation page reads as a receipt; a page that still needs action does not.
- **Density:** for each element, "what would the user lose if it were gone?" Nothing lost means cut it. At most one short hint per field; no stacked hint paragraphs, no welcome text, no restating the button or heading. Instructions are a last resort.
- **Salience:** one filled primary button per screen. At most 3 type sizes, 2 large elements, 3 contrast levels. Squint test: `skills/naive-ui-review/squint.sh <png>` writes blurred copies at 5, 10 and 20 px; the primary element must still dominate. Text contrast at least 4.5:1 (3:1 large).
- **Effort:** count decisions, not clicks (one thoughtful click costs about three mindless ones). Pre-fill the common value. Users' words, not system terms.

## Scoring and output

Pool all runs, dedupe across screens and viewports, then drop non-issues yourself (expect a quarter false positives: misread elements, banners taken for buttons, guidelines over-applied). Rank by severity, tie-break by how many runs flagged it.

Report per round, and keep the numbers to compare rounds:

- comprehension answers correct (share),
- screens rated "very confusing" by at least 2 of 3 runs,
- top findings, each with screen, quote from the reviewer, and the proposed change.

Findings feed the next design brief like founder notes do. Show the founder the round only after the naive pass, with its numbers.

## Limits

- Recall against real users is low (0.21-0.38 in three studies). Strong on copy, spacing and alignment; weak on cross-screen and timing issues. It finds some confusions earlier and cheaper, not all of them.
- Studies used older models and mostly phone screens. Human reviewers also agree on only 5-65% of problems.
- When stakes are high, 3-5 real users beat any number of runs.

Sources: Zhong CW 2025 (arxiv 2512.03568, prompts in Appendix A); Duan CHI 2024 (2403.13139); Lubos 2026 (2508.16165); OPeRA (2506.05606); NN/g on synthetic users, visual hierarchy, interaction cost, UI copy; GOV.UK Design System question pages, check answers, button; Porter 2003 on the three-click rule; WCAG 1.4.3. Full reports: `~/Repos/research-agent/reports/fe8e1f9e416742aa86c3c015a1da2a7d.md`, `c12c0c5dbdeb43099e3e51b287f6b7d3.md`.
