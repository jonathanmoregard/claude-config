---
name: screenshot-review
description: Use when the user wants to review or give feedback on a folder of screenshots or images — walking them one at a time and dictating or typing notes per image — or asks to "run/open the screenshot review tool", "open screenshots for feedback", or "let me review the screenshots". Project-independent; works on any image folder.
---

# Screenshot review

A dependency-free local tool for walking a folder of images one at a time and
capturing per-image feedback (voice or typed) into a single markdown document.
Built for a push-to-talk dictation workflow (e.g. Voquill).

## Launch (any project, any image folder)

```sh
~/.claude/skills/screenshot-review/serve.sh <image-dir> [port]
```

Stages the folder, starts a local static server, and opens the tool in the browser
**already showing the images** (no manual pick). Prints the URL and server PID.
Example: `~/.claude/skills/screenshot-review/serve.sh docs/brand/screenshots/major`.
Stop it with `kill <pid>` or `fuser -k <port>/tcp`.

The tool itself is `tool/index.html` in this skill — it also runs by just opening
that file and clicking **Load folder…** (Chromium-family), but the launcher is the
one-command path and works in any browser.

## Workflow the user follows

1. Image 1/N fills the width; scroll to read. **Fit / Width / −/+** or **Ctrl+scroll** to zoom.
2. Dictate (hold push-to-talk) or type — text lands in the focused box.
3. **›** or **Ctrl+→** advances the image AND inserts a `## <folder>/<file>` heading,
   so the transcript self-organizes one section per image.
4. **Copy all** / **Download .md** at the end. The user pastes it back; then act on
   the feedback per image.

Notes autosave to `localStorage` per folder. Fully local — no upload, no build step.

## Element pinning (Figma-style comment-on-element)

When a `<base>.dom.json` sidecar sits next to `<base>.png` (visual-verify emits
these), pin mode (default ON, toggle with `a` or the 📌 button) hover-highlights
the DOM element under the cursor with its `tag.class "label"`. Click → type a
note → Enter. The pin lands in two places:

1. **Transcript** — a `- 📌#N [button.c-btn "CREATE A POLL"] @(x,y): note` line
   under the image's `##` heading (survives Copy all / Download .md).
2. **`annotations.json` in the SOURCE folder** — written via `POST /save`,
   structured: `{ pins: { "<image-base>": [ {n, x, y, el: {desc, label, rect,
   role}, note} ] } }`. Coordinates are in the dom.json coordinate space
   (CSS px at capture width).

Images without a dom.json sidecar still pin at pixel coordinates (`el: null`).

**Agent contract — consuming pins:** read `annotations.json` from the reviewed
folder. Each pin is a pre-atomized feedback item ALREADY matched to an exact
element — feed `el.desc` + `el.label` + `note` straight into the feedback ledger
as one row each; grep templates for the class in `el.desc` to locate the source.
No interpretation guessing on WHICH element the user meant.

To prep a folder for element-exact review: capture surfaces with visual-verify,
then copy `fullpage-1280.png` → `<surface>.png` and `fullpage-1280.dom.json` →
`<surface>.dom.json` into one flat folder; serve it.

## Requirements
- `python3` (static server) and `xdg-open` (or open the printed URL yourself).
- Any modern browser for the launcher's auto-load; the **Load folder…** picker
  fallback needs a Chromium-family browser.
