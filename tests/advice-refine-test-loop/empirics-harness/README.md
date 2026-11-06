# Empirics harness — advice-refine-test-loop

## Purpose

Measure the false-positive rate of the arftl pipeline on real diffs so gating
decisions can be justified by numbers, not vibes. Two deferred gates are
waiting on data from this harness before we add them to the skill:

- **Confidence threshold** on advisor findings (drop below-threshold before
  Stage 4.5). Deferred until the harness shows a threshold that reduces FP
  rate without hurting TP rate.
- **FP category taxonomy** (buckets like `hallucinated-file`,
  `stale-state`, `subjective-taste`) so the loop can route each class to
  the right suppression mechanism. Deferred until the harness reveals which
  categories actually dominate.

Both are explicitly OUT of the current skill; do not add them speculatively.

## How to add a labeled diff

Create a fixture directory under `fixtures/`:

```
fixtures/<slug>/
  diff.patch          # unified diff, applies cleanly to some known base
  labels.json         # { "<finding_id_hash>": "TP" | "FP", ... }
  README.md           # (optional) provenance: source repo, base SHA, what the diff does
```

`finding_id_hash` is a stable hash of `(file, line, rationale)` so a finding
can be re-identified across pipeline versions. The hash function will be
defined once `run.sh` is implemented; until then, use `sha256(file + "\0" +
str(line) + "\0" + rationale)[:12]` as the placeholder.

Label each finding the pipeline emits on this diff as `TP` (true positive —
real issue the diff introduces) or `FP` (false positive — hallucination,
stale-state, or subjective claim we don't want to surface).

## How to compute FP rate delta

```
./run.sh --diff fixtures/<slug>/diff.patch --pipeline baseline  --out /tmp/baseline/
./run.sh --diff fixtures/<slug>/diff.patch --pipeline candidate --out /tmp/candidate/
```

For each output, join findings against `labels.json`:

- **TP rate** = |findings labeled TP| / |all TP-labeled findings across pipeline runs on this diff|
- **FP rate** = |findings labeled FP| / |all findings emitted|

The candidate pipeline WINS iff:

- FP rate strictly decreases, AND
- TP rate does not decrease (i.e. we didn't suppress real bugs).

Report the delta per-fixture and aggregated across all fixtures. Aggregation
should be macro-average (per-fixture rates averaged) not micro-average
(pooled findings) — one large fixture shouldn't dominate.

## Fixture curation

Aim for diversity along axes the loop is expected to handle:

- Backend correctness (auth, schema, resource management)
- Frontend/UI (template changes that trigger Phase 4)
- Refactors (renames, moves — should stress the `gated_auto` class)
- Pure docs (should trigger the specialist-skip fast path)
- Adversarially-crafted diffs that intentionally invite hallucinations
  (e.g. reference a nonexistent module in a comment to see if the advisor
  invents findings around it)

## Status

Scaffolding only. `run.sh` currently prints a usage stub and exits 1.
