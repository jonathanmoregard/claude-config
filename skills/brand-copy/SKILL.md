---
name: brand-copy
description: Use when writing, editing, or reviewing marketing or product copy for a project that has a brand config under docs/brand/ (voice-config, copy-rules.toml, workbook) — heroes, subheads, microcopy, empty states, ads, emails, site pages, any language.
---

# Brand copy generation (config-driven)

Nothing brand-specific lives in this skill or its tool. Each project supplies its own
configuration under `docs/brand/`:

- `copy-rules.toml` — MECHANICAL rules (banned phrases, dash policy, competitor-name
  allowlists, warning regexes, burstiness). Enforced by the bundled linter.
- a voice config (e.g. `voice-config.md`) — judgment layer: voice DNA, positive
  exemplars, negative exemplars as (draft → fix → why) triples, per-surface register.
- a workbook — decision log: LOCKED copy, killed items with reasons, Later/TBD.

If the project lacks these, offer to scaffold them (the marketing-playbook skill
produces the content).

## Before drafting (mandatory, in order)

1. Read the project's voice config in full. The negative exemplars' WHY lines are the
   steering signal; steer away from each DRAFT pattern.
2. Check the workbook: LOCKED copy on this surface (don't contradict), Later/TBD
   (don't re-draft parked items unasked), standing copy rules.

## Drafting loop

1. Draft against the voice config.
2. Lint every candidate before presenting:
   `python3 ~/.claude/skills/brand-copy/lint.py --rules <project>/docs/brand/copy-rules.toml --text "<candidate>" [--as-file <target path>]`
   ERRORs: fix always. WARNs: fix or consciously keep; if kept, tell the user the
   warning and why.
3. Present in numbered batches of ≤5 for verdicts; accept terse replies
   ("1y 2n 3 rewrite: ..."). One batch at a time.
4. Multilingual projects: every asset is a language pair/set, each approved
   separately. Never auto-translate copy already locked in another language.

## After verdicts (mandatory write-back)

1. Workbook: mark APPROVED/KILLED with date; move approved into locked copy.
2. Voice config: append (draft → fix → why) to negative exemplars and newly locked
   lines to positive exemplars. If the why is unclear, ask — the why is the value.
3. If a verdict implies a new MECHANICAL rule, encode it in `copy-rules.toml` (never
   only in prose) and re-run the linter on the batch.
4. Commit doc updates (default; skip only if the user asked to peek first).

## Voice fidelity (when the goal is "sounds like the author", not just on-brand)

Beyond the rule linter, a stylometric detector scores whether a draft sits inside the
author's own statistical variation and emits targeted feedback:
`python3 ~/.claude/skills/brand-copy/style-detector.py CORPUS DRAFT` (also `--json`,
`--self-test`). Run the draft→score→fix→rescore loop per `adversarial-refine.md` in this
dir; stop when the feedback list empties (don't chase a passed detector's noise). This is
the locally-runnable stylometric SOTA; the neural/perplexity detector families (torch +
models) are the documented optional next rung. `style-divergence.py` is the older, simpler
scorer — prefer `style-detector.py`.

## Linter engine note (decided 2026-07-08)

The bundled `lint.py` is deliberately hand-rolled (~150 lines, stdlib-only): the
config is the asset, the engine is swappable. Migrate to Vale (nixpkgs `vale`) when
ANY tripwire hits: (1) linting whole markdown files rather than copy snippets
(markup-aware scoping becomes load-bearing — lint.py false-positives on code blocks/
URLs), (2) rule count grows past ~2 dozen or needs per-surface variation, (3) CI or
editor integration wanted. Migration: port copy-rules.toml → Vale styles YAML, keep a
small burstiness script alongside (Vale can't do sentence-stdev natively), update the
lint command above.

## Never

- Show a candidate with linter ERRORs, or lock copy without explicit approval.
- Put humor outside the surfaces the voice config designates as free-play zones.
