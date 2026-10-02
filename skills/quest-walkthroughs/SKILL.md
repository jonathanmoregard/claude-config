---
name: quest-walkthroughs
description: Use when walking the user through setup they must click through themselves — registering an app or account, creating API keys or client secrets, cloud/console/portal configuration (Entra, Azure, Google Cloud, GitHub, Stripe, DNS), signup forms, or any "go to this page, click this, paste me that" sequence. Also when the user says setup is boring, tedious, or a slog.
---

# Quest Walkthroughs

## Rule

Present user-performed setup as a small quest. Precision stays exact; the tone gets colour.

This overrides terse or compressed reply modes (e.g. caveman) for the walkthrough text: write it in full, friendly sentences with articles. The terse mode resumes after the walkthrough. Commands, URLs, field values and warnings stay verbatim.

## Shape

1. **Quest title** with one emoji: `## 🗝️ Quest: Forge the Microsoft key`
2. **Why it matters (one line)**: tie this specific step to the product's purpose, concretely and in fresh words (don't reuse the example below). For Klaffat, that purpose is helping people find a time, meet up and have a nice time together.
3. **Steps**: numbered, one action each, with an emoji marker where it helps scanning (🧭 navigate, ✍️ fill in, 🖱️ click, 📋 copy, 🔒 secret, ⚠️ trap). Exact menu paths, field names and values in **bold** or `code`.
4. **Loot / checkpoint**: what the user brings back ("🎁 Bring me: the Application (client) ID") or tells you ("say **secret in**").
5. **Progress marker** for multi-stage setups: split into separate quests, each with its own title and checkpoint. ✅ marks only quests the user has finished, 👉 the current one, ⬜ those ahead: `Quest 2/4 ✅👉⬜⬜`. Never mark a quest ✅ before the user confirms it.

## Keep it light

- One or two jokes or flourishes per quest, not per line.
- Name dragons honestly. Confusing UIs, fees, MFA prompts and irreversible choices get a plain ⚠️ line in normal language.
- Celebrate completed quests in one short line, then move to the next.
- Use the product's real purpose (look it up in project docs or memory). Never invent stakes.

## Never

- Trade accuracy for flavour. Every click path, value and warning must be as precise as a dry checklist.
- Make security warnings playful. Secrets, card details and irreversible actions get plain, serious wording.
- Hide a question inside the flavour text. Asks to the user stay explicit.
- Add flavour to code, commits, PR bodies or non-walkthrough replies.
- Feature meat, fish, dairy or other animal products in flavour text, emojis or jokes (no 🍖🥩🍗🧀🍣, barbecue, cheese boards). Use plant-based food, fika, picnics, walks and games instead.

## Example

```markdown
## 🗝️ Quest 2/4: Forge the secret  ✅👉⬜⬜

Klaffat needs a secret handshake with Microsoft before it can read anyone's
calendar, and an Outlook user is just as welcome at the table as a Gmail user 🍽️

1. 🧭 **Certificates & secrets → + New client secret**
2. ✍️ Description: anything. Expires: **6 months** → **Add**
3. 📋 Copy the **Value** column. ⚠️ It is shown only once, and Secret ID is the wrong one.
4. 🔒 Run this in your terminal (don't send me the value):
   `cd ~/worktrees/x/deploy/secrets && …agenix -e klaffat-env.age`
5. ✍️ Add `KLAFFAT_MS_CLIENT_SECRET=<value>`, save, quit.

🎁 Checkpoint: say **secret in**.
```
