---
status: proposed
category: security
date: 2026-04-16
source: manual
project: global
---

## Problem

Current untrusted-ingress wrapping (`wrap-untrusted-tavily.py`,
`wrap-untrusted-bash.py`) tags content by *source* — it tells Claude
"this came from the web / from `curl`" but does not look at the content
itself.

Adversary can still slip prompt injection past source-tagging via
*visual stealth* — text the model sees but a human review wouldn't
catch:

- **Zero-width characters** (`U+200B`, `U+200C`, `U+200D`, `U+FEFF`)
  splitting instruction keywords across invisible boundaries.
- **RTL override / bidi control** (`U+202E`, `U+2066`–`U+2069`) hiding
  directives inside innocent-looking text.
- **Homoglyphs** — Cyrillic `а` for Latin `a`, etc. — used to bypass
  keyword-based filters or to reconstruct "ignore previous
  instructions" in a form the model parses but a grep doesn't.
- **Base64 / hex blobs** embedded in retrieved content; the model may
  decode and follow them.
- **Unicode tag characters** (`U+E0000`–`U+E007F`) — invisible,
  machine-readable instruction smuggling. Documented prompt-injection
  vector.

Tweet (affaanmustafa 2026-04-16) and Anthropic's indirect-injection
write-up both call out hidden text as a real attack channel.

## Proposal

Extend the two existing PostToolUse hooks (Tavily, Bash) with a
content-pass before emitting the wrap:

1. Scan the tool response for:
   - Zero-width chars, bidi overrides, unicode tag chars → flag and
     **strip** before the text reaches context. Stripping is safer
     than flagging because a flagged-but-intact payload still reaches
     the model.
   - Homoglyph density above a threshold (mixed-script words) → flag
     in the wrap reminder.
   - Long base64/hex runs (> N chars, high entropy) → flag with
     count + decoded-length preview in the wrap reminder. Do not
     auto-decode into context.

2. Emit enriched `additionalContext` listing what was stripped / what
   was flagged, so the model knows the content was sanitized.

3. Extract the sanitizer into a shared module:
   `~/.claude/security/content_sanitize.py` — imported by both wrap
   hooks. Keeps rules in one place; new ingress hooks reuse it.

Out of scope for first pass:
- Multimodal (PDF/image) stealth. Requires OCR + visual inspection;
  tracked separately as B3.
- `Read` on downloaded files. Adds a new hook; ship content-sanitizer
  first, then reuse.

## Why strip-not-just-flag

Flagging alone depends on the model noticing and respecting the flag.
Stripping is structural — the payload never reaches context. The one
class we flag-not-strip is base64/hex, because those frequently appear
in legitimate output (git SHAs, keys in test fixtures).

## Research

- `/home/jonathan/.claude/research/security-2026-04-16-prompt-injection-posture.md`
  — posture doc
- Anthropic, "Defending against indirect prompt injection attacks" —
  hidden-text specifically called out
- OWASP LLM Top-10: LLM01 Prompt Injection, "obfuscated input"
