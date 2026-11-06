---
name: copywriting
description: Use when asked to write, rewrite, or punch up prose the user will publish or send — blog posts, blurbs, announcements, emails, social copy — especially "in my voice", "make it sound like me", or "don't make it sound like AI". Also triggers on reviewing existing publishable copy for AI tells — "verify it doesn't look like AI slop", "check the store listing copy", "does this read as AI-written?".
---

# Copywriting — voice registry + pipeline picker

Front door for copy work. Voices are TOML files in `voices/` next to this file — reading
that directory IS the registry; never hardcode voice names. Shared tools live in
`~/.claude/skills/brand-copy/` (lint.py, style-detector.py, neural_channel.py,
voice_generate.py, adversarial-refine.md).

## Step 1 — read the registry

Read every `voices/*.toml`. A voice may have `[local]` (corpus + configs for
Claude-drafts-then-verify), `[neural]` (fine-tuned generator endpoint), or both.

## Step 2 — ONE AskUserQuestion call

- **1 voice in registry:** single question, one option per available pipeline plus the
  fallback, e.g.: "blog — neural (fine-tune generates, dual scoring) (Recommended)",
  "blog — local (Claude drafts, stylometric gate)", "No voice — anti-slop rules only".
- **2+ voices:** two questions in the same call: Q1 voice (one option each, from TOML
  `description`, plus "None — anti-slop only"), Q2 pipeline (neural / local; note it's
  ignored if Q1 = none).
- Recommend neural whenever the voice has it.
- Skip the question entirely only if the user already named voice+pipeline in the request.

**Craft doc:** if the chosen voice's TOML has a `craft` key, use that file as a
VALIDATION step — check drafts against it and report violations to the user. Do NOT use
it as a generative recipe; checklist-driven drafting produces formula (user feedback
2026-07-13). For essays: prose style only — no numbered protocols or listicle sections
unless the user's own material has them. If the user rejects a generated draft twice,
stop regenerating and hand the text back to them with the validation findings instead.

**Universal pre-draft research (MANDATORY, every pipeline, every time):** before writing
any source draft, run `mcp__research-agent__research` (warn the user about the wait;
depth `fast` for a quick fact check, `normal`/`deep` when the piece makes arguments).
Cover: (1) verify every proper noun, product/game name, quote, and factual claim in the
brief — dictated briefs garble names; (2) validate the piece's argument against the
relevant field's state of the art; (3) surface established terminology and prior art
worth weaving in or nodding to. Fold findings into the draft; flag any claim the research
could only support as folk wisdom, and soften or label it.

**Universal gate:** the programmatic anti-slop lint runs on EVERY pipeline and every
candidate, regardless of voice or length:
`python3 ~/.claude/skills/brand-copy/lint.py --rules {voice copy_rules, else ~/.claude/skills/copywriting/anti-slop-rules.toml} --text "..."`
Fix ERRORs always; keep a WARN only consciously and disclose it.

## Step 3 — run the chosen pipeline

### Neural (voice has `[neural]`)
1. Get a source draft: the user's pre-written draft if one exists, else Claude writes a
   strong draft from the brief (substance first — do NOT imitate the voice; the fine-tune
   does voice). The fine-tune alone writes in-voice but substantively weaker — bare-brief
   solo generation is a fallback only if the user says voice matters more than substance.
2. Prompt file = "Rewrite the following essay in your own natural voice. Keep every idea
   and the structure; do not summarise or shorten." + the draft. Empirical (2026-07 eval):
   rewrites anchor to the source's style — expect ~1/6 candidates in-band per round, so
   n=6 minimum and be ready for a second round. If a round yields 0 in-band, regenerate
   at temperature 1.0 (empirical: 0/6 at 0.9 → 5/6 at 1.0) — but higher temperature also
   raises hallucination risk, so fidelity-screen every candidate (grep for key names/
   sections) before reading the best one; in-band scores say nothing about content.
   Don't describe the author's style in the prompt.
   Generate (cold start can take minutes — run detached, see detached-long-commands skill):
   `VOICE_ENDPOINT={endpoint} python3 ~/.claude/skills/brand-copy/voice_generate.py --prompt-file P.txt --n 6 --temperature 0.9 --max-tokens M --timeout 1800 --outdir OUT/`
   where `M ≈ target_words × 1.7 + 200`; the endpoint context is 4096, so keep
   prompt tokens (≈ prompt_words × 1.3) + M under 4000 — trim the prompt if it doesn't fit.
3. Score every candidate: source the voice's `score_env`, then
   `python3 ~/.claude/skills/brand-copy/neural_channel.py score CAND --json`
   (want `channels.style.inside == true`), plus if `[local]` exists
   `python3 ~/.claude/skills/brand-copy/style-detector.py {corpus} CAND` (want 21/21),
   plus the universal lint gate on each candidate.
   `score_env` may `cd` elsewhere — always pass candidate paths as ABSOLUTE paths.
4. Present the best candidate passing both gates. Split verdicts: neural in-band but
   local <21/21 → apply the detector's FEEDBACK fixes to that candidate, re-score both.
   Local passes but neural below-band → regenerate; do NOT hand-edit toward the author
   (empirically that moves StyleDistance the wrong way). None in-band anywhere → refine
   the best via `~/.claude/skills/brand-copy/adversarial-refine.md`, or regenerate.
5. Known model quirk: it inlines/drops `[^n]` footnotes — restore by hand.

### Local (voice has `[local]`)
1. Read the voice's `voice_config` in full first (negative-exemplar WHY lines steer).
2. Draft yourself, then run the universal lint gate.
3. Run the adversarial loop per adversarial-refine.md at any length
   (`style-detector.py {corpus} DRAFT`, apply its FEEDBACK, re-score, stop at 0 fixes —
   don't iterate past a pass). On very short copy some features are noisy — treat the
   FEEDBACK list as the signal, not the raw in-band count.
4. Present in numbered batches of ≤5; accept terse verdicts ("1y 2n").

### Anti-slop only (no voice)
Draft, then the universal lint gate (with `anti-slop-rules.toml`). That's the whole gate.

## Adding a voice (drop-in, no skill edits)

Create `voices/<name>.toml`:

```toml
description = "shown as the AskUserQuestion option"
[local]                    # optional — enables the local pipeline
corpus = "~/path/archive.md"        # style-detector reference corpus
voice_config = "~/path/voice.md"    # judgment layer (exemplars)
copy_rules = "~/path/rules.toml"    # lint config; omit to use anti-slop-rules.toml
[neural]                   # optional — enables the neural pipeline
endpoint = "https://...modal.run"   # OpenAI-compatible
model = "voice"
score_env = """shell lines exporting scorer keys/env"""
generate_cmd = "..."       # optional override if voice_generate.py doesn't fit
score_cmd = "..."          # optional override if neural_channel.py doesn't fit
```

A voice with only `[local]` simply never offers neural (and vice versa). To mint a new
neural voice end-to-end (data prep → QLoRA on Modal → deploy), follow the runbook:
`~/Dropbox/Blogg/docs/brand/finetune/PLAN.md`.

## Common mistakes

- Hardcoding "blog" anywhere — always enumerate `voices/*.toml`.
- Skipping the lint gate on any pipeline — it is programmatic and runs on every path.
- Asking neural-vs-local as a bare question with no recommendation.
- Describing the author's style in the neural prompt (fights the fine-tune).
- Printing decrypted keys; `score_env` exists so keys stay out of transcripts.
