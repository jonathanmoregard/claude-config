---
name: playground-review
description: Use when the user wants to review LIVE interactive component playgrounds (not screenshots) — walking one at a time and dictating/typing notes per playground — or asks to "open the playground review tool", "review the playgrounds", "give feedback on the interactive components". Sister to screenshot-review; iframe-based instead of image-based.
---

# Playground review

When the review target is a running dev server's playground page (interactive, click/hover/focus actually work), use this tool instead of `screenshot-review`. It iframes the live page + collects notes per URL. Element-pin (📌 toggle) works when the host page cooperates — see "Same-origin embedded mode" below; the notes textarea is per-playground and autosaves either way.

## Launch — standalone (cross-origin fallback)

    ~/.claude/skills/playground-review/serve.sh <playgrounds.json> [port]

Where `<playgrounds.json>` is a file OR a directory containing `playgrounds.json`. Schema:

    [
      { "name": "c-btn", "url": "http://127.0.0.1:3737/dev/components/btn?variant=destructive&hover=1" },
      { "name": "c-dialog", "url": "http://127.0.0.1:3737/dev/components/dialog?variant=danger&open=1" }
    ]

Order in JSON = display order. `name` labels the sidebar entry + becomes the transcript `##` heading.

## Same-origin embedded mode (preferred when the host cooperates)

Cross-origin iframing is fragile: CSP `frame-ancestors`, sticky `chrome-error://` state, cache mismatches, `about:blank` load races, and lost postMessage handshakes all become non-issues if the tool + iframe share an origin. Any host server can embed the tool by:

1. Serving `~/.claude/skills/playground-review/tool/index.html` verbatim at some path (e.g. `/dev/pgr/`).
2. Injecting the manifest inline before `</head>` as:

        <script id="pgr-manifest" type="application/json">
          [{"name":"c-btn","url":"/dev/components/btn?variant=destructive"}, ...]
        </script>

   URLs may be relative — the tool normalises against `location.origin`.

The tool prefers this inline manifest over `fetch('/playgrounds.json')` when present. No `serve.sh`, no separate port. Kablong wires this at `crates/kablong-web/src/handlers/dev_pgr.rs` and mounts it under the `dev-playground` feature.

## Workflow

1. Tool opens; left sidebar lists playground names, main pane iframes the first URL.
2. Interact with the playground normally — click, hover, focus, type in its controls. State survives the session (URL controls hydrate on reload).
3. Type notes in the textarea below (or dictate). Notes autosave per playground under a `##` heading.
4. `←`/`→` (bare, when notes not focused) or click sidebar to switch playgrounds.
5. Copy all / Download markdown at the end.

## Per-playground source snapshot (html + css + js)

Every playground the reviewer opens is snapshotted automatically ~400 ms after
its iframe loads: post-render DOM, the CSS that applied, the JS that shipped.
The `</> source` button in the toolbar re-captures the current one on demand
(after you've interacted with it, to catch the mutated DOM).

- **Standalone mode** — the three files are written next to `playgrounds.json`
  as `<name>.source.html` / `.source.css` / `.source.js` (server `POST
  /pgr-source`). The toolbar's saved-line confirms what landed.
- **Same-origin embedded mode** — no write-back endpoint, so the button falls
  back to browser downloads (three files into your download dir). Hosts that
  want automatic write-back must expose their own `POST /pgr-source`.
- **Cross-origin fallback** — `contentDocument` is null, so nothing can be
  read. Capture with `visual-verify` against the same URLs instead; it emits
  the identical `<shot>.source.*` trio.

**Agent contract:** read `<name>.source.html` for the playground a note was
written against, locate the element the note describes, then grep templates for
that markup/class to reach the generating file. `.source.css` shows which rule
actually produced the spacing/color complained about, cascade order included.
Never propose a fix from the served template alone — these playgrounds mutate
their own DOM.

## When to use

- `/dev/components/<slug>` interactive live previews (kablong Ph-5)
- `/dev/pages/<slug>` page-pattern playgrounds
- Any dev-server surface where the review is about HOW IT FEELS to interact, not just how it looks

## When NOT to use

- Static-image reviews → `screenshot-review`
- Full-surface UI reviews of shipping pages → `visual-verify` + `screenshot-review`

## Requirements

- Standalone mode: `python3` (static server), `xdg-open` (or open the printed URL yourself)
- Same-origin embedded mode: the host server serves `tool/index.html` with the inline `<script id="pgr-manifest">` block (see above)
- The dev server hosting the playgrounds must already be running (this tool doesn't boot it)
- Element-pin (📌) requires the iframed pages to load `dev-playground-pin.js` (kablong does; other hosts must opt in)
