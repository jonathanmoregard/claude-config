---
name: htmx
description: Use when adding or reviewing HTMX or hx-* behavior; replacing fetch, client-rendered HTML, JavaScript form submission, polling, loading states, or client routing with fragments; or checking HTMX XSS, CSRF, authorization, and fragment safety.
---

# HTMX

Server renders HTML; HTMX transports and swaps it. Keep native HTML working without JavaScript where practical.

## Replace these patterns

| If code does this | Use this instead |
|---|---|
| `fetch` JSON, then builds HTML or assigns `innerHTML` | Autoescaped partial + `hx-get`/`hx-post`, `hx-target`, `hx-swap` |
| `preventDefault()` + custom form submission | Real `method`/`action` + `hx-post`; return swapped HTML |
| `setInterval(fetch...)` for server state | `hx-trigger="every …"` or SSE event triggering fragment refresh |
| Manual spinner/button disabling | `hx-indicator` + `hx-disabled-elt` |
| `AbortController`/stale-response guards | `hx-sync="this:replace"` on competing requests |
| Client router for ordinary links/forms | Targeted `hx-boost` + `hx-push-url`; keep valid URLs |

Keep JavaScript for local interaction, browser APIs, and HTMX lifecycle glue—not server transport or HTML construction.

## Security boundary

- Call only same-origin application routes. Never place untrusted data in `hx-*` URLs, selectors, headers, vals, or event code.
- Use autoescaping templates. Put untrusted content only in element bodies as text. Never mark user-controlled HTML `safe`/raw.
- Authenticate and authorize every endpoint. Keep CSRF/Origin checks and rate limits. `HX-Request` is only a rendering hint; send `Vary: HX-Request` when it selects page versus fragment.
- Return exact `text/html` fragments. Treat `hx-disable` as defense-in-depth, not sanitization.
- Configure `selfRequestsOnly: true`, `allowEval: false`, `allowScriptTags: false`, and preferably `historyCacheSize: 0`. Use strict CSP; set session cookies `Secure`, `HttpOnly`, `SameSite=Lax`.

Before shipping, test escaped hostile text, no-JS form fallback, target scope, focus restoration, authorization, and CSRF rejection.
