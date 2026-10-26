# Adversarial style-refinement loop

Detector-vs-generator co-refinement: draft in an author's voice, score against a
divergence detector, act on its feedback, repeat until the draft sits inside the
author's own statistical variation. Distilled from 2026-07 SOTA research on AI-text
detection and style-transfer generation (reports in `~/Repos/research-agent/reports/`:
`07e905d6…` detectors, `262ce984…` generators).

## The two tools

- **Detector — `style-detector.py CORPUS DRAFT`** (pure stdlib, this dir). 21-feature
  stylometric ensemble: Cosine Delta (unigram + word-trigram MFW), function-word
  chi-square, sentence-length mean/stdev/KS, MTLD + Yule's K, char 3/4-gram cosine,
  9 punctuation rates, sentence-opener share, AI-phrase-tell scan. Calibrated so
  "in-band" = inside the author's OWN chunk-to-corpus spread (not z≈0). Emits per-feature
  verdicts AND a consolidated FEEDBACK list = the generator's to-do. `--self-test`
  proves it doesn't flag the author's own held-out writing (must stay ~>90% in-band).
  `--json` for programmatic loops.
- **Generator — Claude (you).** Drafting is where taste lives; keep it in the main agent,
  not a tool. The detector's feedback strings name exactly what to fix.

## The loop

1. Draft in-voice (read the project's `voice-config.md` first; use its positive +
   negative exemplars — the negative-exemplar *why* lines are the steering signal).
2. `python3 style-detector.py CORPUS DRAFT` → read the FEEDBACK list.
3. If feedback is non-empty: apply the named fixes (targeted rewrites, not a full
   redraft — merge choppy sentences, cut the specific over-used tell, vary openers).
   Re-score. This is the ramp.
4. Stop when the feedback list is empty AND the verdict is ~all-features in-band.
   Convergence = the detector has no actionable complaint. Do NOT keep iterating past
   this — both research reports warn that chasing a passed detector's residual noise
   overfits and degrades readability ("past ~8 passes you're chasing noise: stop").

Worked result (form-is-emptiness essay): naive AI draft 67% in-band (7 fixes) →
4 refinement rounds → 100% in-band (0 fixes); author's own held-out writing scores 96%.

## Boundary — detector families (avoid overfitting ONE)

Both reports' central warning: evading one detector leaves artifacts another family
catches. `style-detector.py` is the **stylometric family** — and per the detector
research it is the *hardest to fool for a specific author* (Cosine Delta at high MFW +
function-word distributions resist deliberate manipulation). But it is one family. The
other two, deliberately NOT built here because they need torch + model downloads + a
GPU (heavy, possibly infeasible in this env):

- **Neural authorship embeddings** — StyleDistance (`StyleDistance/styledistance`,
  content-independent) + LUAR (`rrivera1849/LUAR-CRUD`). Score = cosine(draft, corpus
  centroid). Their *disagreement* is diagnostic (topic-right but shape-wrong trips
  StyleDistance while satisfying LUAR).
- **Perplexity / curvature** — Binoculars (Falcon-7B or Llama-3-8B base+instruct pair,
  threshold ~0.85) is zero-shot SOTA; Fast-DetectGPT (single 8B) is the cheap runner-up.
  These detect "LLM-generated" in general, not "not-this-author".

To close the ensemble: `pip install transformers torch sentence-transformers`, add a
neural-score wrapper, fold its score in as an extra fitness channel (weight all families,
never optimize one). Until then, treat a 100% stylometric pass as "clears the
locally-runnable SOTA detector, neural channel untested." A web detector (GPTZero etc.)
is an upload-to-third-party call the user makes, not this loop.
