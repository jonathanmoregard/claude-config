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

## Requirements
- `python3` (static server) and `xdg-open` (or open the printed URL yourself).
- Any modern browser for the launcher's auto-load; the **Load folder…** picker
  fallback needs a Chromium-family browser.
