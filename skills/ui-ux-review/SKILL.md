---
name: ui-ux-review
description: Use when reviewing how an app's screens actually look, doing a UI/UX or visual-design pass, gathering per-screen design feedback, or when the user says "review the UI", "do a UX review", "let's go through the screens", "review the screenshots of the app", or wants to give feedback on rendered surfaces one at a time.
---

# UI/UX review loop

Review how the app *looks* by rendering every major surface to an image, letting the
user give feedback per screen, then turning that feedback into concrete diffs and
**re-rendering to confirm the change looks right** — not eyeballing code.

**Screens are the source of truth for appearance.** Always re-screenshot after a
change; never assume a CSS/template edit produced the intended look.

## The loop

1. **Capture a comprehensive, current set.** Use the project's screenshot harness
   (e.g. Playwright `fullPage` specs); if none exists, add one. One image per major
   surface, labelled by role (e.g. `admin-`/`guest-`/`system-`), written to a single
   folder. Keep an `INVENTORY.md` listing every surface — **a screen not captured is a
   screen not reviewed.** Refresh the set against the current build before each round.
2. **Serve it for review.** REQUIRED TOOL: use the **screenshot-review** skill —
   `~/.claude/skills/screenshot-review/serve.sh <image-dir>` opens the images one at a
   time with per-image feedback capture (voice or typed).
3. **User reviews → returns a markdown doc** whose `## <folder>/<file>.png` headings
   delimit one section per image, feedback beneath each.
4. **Apply per-screen fixes.** For each heading with notes: map the filename to the
   surface behind it (template/component + CSS/handler), make the change, keep the
   project's gate green. Group shared changes (a token/component touched by many
   screens) so you fix once.
5. **Re-render + verify.** Re-run the screenshot spec for the changed surfaces (or the
   whole set) and confirm the pixels changed as intended. Loop until the user signs off.

## Notes that keep rounds honest

- **Comprehensive before pretty.** Get full coverage first; a partial set hides the
  surfaces most likely to be wrong (empty states, errors, closed/expired, mobile).
- **Deterministic feedback→surface mapping.** The heading = the filename = one surface.
  Don't guess which screen a note refers to; the structure tells you.
- **One image per surface, labelled**, so the reviewer isn't lost and diffs are traceable.
- **Taste calls surface with artifacts** — show before/after screenshots for design
  changes before treating them as done.
- **Re-screenshot, don't trust the edit.** A template change can regress an unrelated
  screen; the re-render is the proof.

## When NOT to use
- Pure logic/behaviour review (use tests / code review) — this is about *appearance*.
- A single one-off screenshot glance — just look at it; the loop is for a set.
