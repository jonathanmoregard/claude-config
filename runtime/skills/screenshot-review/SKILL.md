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
then copy `fullpage-1280.png` → `<surface>.png`, `fullpage-1280.dom.json` →
`<surface>.dom.json`, and the three `fullpage-1280.source.*` files →
`<surface>.source.*` into one flat folder; serve it. Copy the source sidecars
every time — a review handover without them cannot be mapped back to markup.

## Per-page source snapshot (html + css + js)

`visual-verify` emits three more sidecars per shot, next to `<base>.png`:

| file | contents |
|---|---|
| `<base>.source.html` | post-render serialized DOM — what the browser actually had, not the served template |
| `<base>.source.css` | every stylesheet that applied, rules as parsed, `/* ===== <href> ===== */` separated |
| `<base>.source.js` | every script that shipped (inline bodies verbatim, external bodies fetched), `// ===== <src> =====` separated |

When they're present, the tool shows `source: html · css · js` links in the top
bar for the current image (new tab). Turn the capture off with `--no-source`.

**Agent contract — consuming source:** a review note ("this button is too
tight", "wrong heading here") is grounded by reading `<base>.source.html` for
the surface it was written against: find the element (pins already give you
`el.desc`, e.g. `button.c-btn`), then grep the repo's templates for that exact
markup / class to reach the file that generates it. `.source.css` tells you
which rule actually produced the spacing or color being complained about —
including cascade order — so the fix lands on the real declaration instead of a
new override. Read the snapshot BEFORE proposing a change; the served template
alone can mislead when JS mutates the DOM.

## Requirements
- `python3` (static server) and `xdg-open` (or open the printed URL yourself).
- Any modern browser for the launcher's auto-load; the **Load folder…** picker
  fallback needs a Chromium-family browser.
