---
name: playground-review
description: Use when the user wants to review LIVE interactive component playgrounds (not screenshots) — walking one at a time and dictating/typing notes per playground — or asks to "open the playground review tool", "review the playgrounds", "give feedback on the interactive components". Sister to screenshot-review; iframe-based instead of image-based.
---

# Playground review

When the review target is a running dev server's playground page (interactive, click/hover/focus actually work), use this tool instead of `screenshot-review`. It iframes the live page + collects notes per URL. No element-pin (cross-origin blocks hit-testing in iframe); the notes textarea is per-playground and autosaves.

## Launch

    ~/.claude/skills/playground-review/serve.sh <playgrounds.json> [port]

Where `<playgrounds.json>` is a file OR a directory containing `playgrounds.json`. Schema:

    [
      { "name": "c-btn", "url": "http://127.0.0.1:3737/dev/components/btn?variant=destructive&hover=1" },
      { "name": "c-dialog", "url": "http://127.0.0.1:3737/dev/components/dialog?variant=danger&open=1" }
    ]

Order in JSON = display order. `name` labels the sidebar entry + becomes the transcript `##` heading.

## Workflow

1. Tool opens; left sidebar lists playground names, main pane iframes the first URL.
2. Interact with the playground normally — click, hover, focus, type in its controls. State survives the session (URL controls hydrate on reload).
3. Type notes in the textarea below (or dictate). Notes autosave per playground under a `##` heading.
4. `←`/`→` (bare, when notes not focused) or click sidebar to switch playgrounds.
5. Copy all / Download markdown at the end.

## When to use

- `/dev/components/<slug>` interactive live previews (kablong Ph-5)
- `/dev/pages/<slug>` page-pattern playgrounds
- Any dev-server surface where the review is about HOW IT FEELS to interact, not just how it looks

## When NOT to use

- Static-image reviews → `screenshot-review`
- Full-surface UI reviews of shipping pages → `visual-verify` + `screenshot-review`

## Requirements

- `python3` (static server), `xdg-open` (or open the printed URL yourself)
- The dev server hosting the playgrounds must already be running (this tool doesn't boot it)
- iframe is cross-origin from the tool's port, so no element-pin — text notes only
