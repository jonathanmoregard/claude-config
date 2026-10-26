---
name: htmx
description: Use when implementing/reviewing HTMX/hx-*; replacing fetch, client HTML, form JavaScript, polling, loading, or routing; or auditing HTMX XSS, CSRF, authorization, and fragments.
---

# HTMX

Server renders escaped HTML; HTMX transports/swaps. Preserve native no-JavaScript links/forms.

## Replace

| Pattern | Use |
|---|---|
| `fetch` + JSON-to-DOM/`innerHTML` | `hx-get`/`hx-post` + autoescaped fragment, `hx-target`, `hx-swap` |
| `preventDefault()` submit | Real `method`/`action` + `hx-post` |
| `setInterval(fetch...)` | `hx-trigger="every …"` or SSE-triggered fragment refresh |
| Manual request state/races | `hx-indicator`, `hx-disabled-elt`, `hx-sync="this:replace"` |
| Router for normal links/forms | Targeted `hx-boost` + `hx-push-url` |

Keep local/browser JavaScript accessible and idempotent after swaps; never replace focused/active widgets.

## Security

- Fixed same-origin routes; no untrusted `hx-*` URLs/selectors/headers/vals/event code.
- Autoescape (`{{ value }}`, never `{{ value|safe }}`); user content only as text.
- Authenticate/authorize; keep CSRF/Origin/rate limits. `HX-Request` only selects rendering; use `Vary: HX-Request`.
- Return `text/html`. `hx-disable` never sanitizes.
- Set `selfRequestsOnly:true`, `allowEval:false`, `allowScriptTags:false`, `includeIndicatorStyles:false`, `historyCacheSize:0`.
- Strict CSP; cookies `Secure`, `HttpOnly`, `SameSite=Lax`.

Test hostile content/attributes, no-JavaScript fallback, content type/Vary, auth/CSRF, CSP/cookies, swap/focus/loading recovery, SSE isolation/rate/reconnect, and local keyboard/status behavior.
