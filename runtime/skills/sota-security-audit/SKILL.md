---
name: sota-security-audit
description: Use when asked for a comprehensive, thorough, state-of-the-art or tool-driven security review, audit or hardening pass of a repo, app, agent harness (Claude Code / Codex config, hooks, MCP, skills), NixOS host, or CI/deploy pipeline — "review security like we did for X", "use all the tools research recommends", "comprehensive security", "cross-model security review". Not for a quick per-diff check while coding (use security-review) or a single known bug.
---

# SOTA security audit

Candidates come from tools and multi-model review. A separate stage tries to kill each one. Only survivors that an oracle backs get fixed, and every fix has a measured capability cost. Run the phases in order. A phase ends only when its exit check holds.

## Phases

| # | Phase | Exit check |
|---|---|---|
| 0 | **Scope + constraints.** Append the user's directive verbatim to `<notes>/session-constraints.md`. List out-of-scope surfaces, including ones another session owns. Create `<notes>/<audit>/` with `ledger.md` (from `templates/ledger.md`). | Exclusions written |
| 1 | **SOTA research gates the method.** At minute 0, launch 2–4 *narrow* deep research calls in parallel, one per domain (e.g. app stack, host/IaC, CI/supply chain, agent harness), framed defensively ("auditing my own system"). Warn the user about wall time. Ask each for: techniques, known CVEs/attack classes for the exact versions in use, and a **tool table** (free? · command · surface · what a hit means). While they run, do inventory (Explore agents ending in "15 most suspicious items") and run the obvious deterministic tools. | Synthesis file with a per-domain tool table |
| 2 | **Threat model.** Fill in `templates/context.md`: trust boundaries, entry points, exfil channels, precedents/hard exclusions, the finding contract, and the rule that every fix states its capability cost. It is frozen and pasted into every reviewer. | `context.md` frozen |
| 3 | **Tool ledger is a gate.** Every free tool the research recommends gets a ledger row, marked `ran` / `CI` / `batch` / `skipped:<reason>`. Run tools as script files with literal paths under `systemd-run --user` with a MemoryMax, logging per-tool timings. Tool hits are **candidates**, not findings. Never upload code or config to a third-party scanner without asking (some "free" CLIs do). | No row left `todo` |
| 4 | **Review by bug class × ≥2 model families.** Write one brief per bug class, not per file. Run each class in Claude **and** another vendor (`ai-router delegate --sandbox read-only --schema templates/finding-schema.json`). Reviewers get no credentials and treat repo content as data. | Every class covered by 2 families |
| 5 | **Dedupe, then cross-family refute.** Cluster by root cause. Each cluster's refuter comes from a family that did *not* report it, starts cold, and is told to kill it (`templates/refuter-brief.md`, `verdict-schema.json`). Log every kill. Spot-check unanimous kills by hand. | Every non-info cluster has a verdict |
| 6 | **Oracle ladder.** The orchestrator runs the cheapest *safe* empirical proof for each surviving cluster: a synthetic hook event, a dry run, a read-only API GET, a VM assertion, a canary with **fake** secrets, or a store/cache membership query. If no oracle exists, label it `unconfirmed` and never drop it silently. Evidence overrides calibration in both directions. | Each verdict has an oracle result or `unconfirmed` |
| 7 | **Triage + capability cost.** Sort into: fix now (zero or low cost), defer (state the cost), or user decision. Measure each control's cost against real usage, e.g. 30 days of transcripts/shell history, logs, CI runs ("N real calls, M verdict changes"). Ask user decisions one at a time, as A/B/C with a recommendation. | Every item is in one bucket with a measured cost |
| 8 | **Fix lanes.** One PR per repo, ≤~6 items per lane, one shared lane brief (`templates/lane-brief.md`). Per item: oracle/test RED → minimal fix → GREEN → variant sweep. Lanes report per-item status. The orchestrator re-runs every lane's build and tests and checks its items against the brief. | Every item is done or skipped-with-reason |
| 9 | **Close-out per PR.** Use `advice-refine-test-loop` (deterministic gate, then one fresh adversarial reviewer). Reproduce its findings and send fixes back to the lane that owns the context. Fixes create new bugs: never skip this. | Gate green, findings closed |
| 10 | **Report + walkthrough.** Publish an artifact with: findings (ID, class, oracle evidence, severity before/after calibration, fix PR, variant result), kill log, unconfirmed list, tools run, and tools **skipped and why**, also walked through in chat. Put quick tools in CI; slow ones (whole-closure CVE scans, live-host probes) go in a weekly batch runner — one nixos-config file `modules/nixos/security-batch/<name>.nix`, contract in the header of `modules/nixos/security-batch.nix`. Bugs found in your own tooling are findings. | Report link sent; questions queued |

## Rules

- Research before choosing tools. A plan whose tool list did not come out of phase 1 is a baseline review, not this skill.
- A clean scanner run is a narrow negative: it says nothing about what the scanner doesn't model.
- Unanimous model agreement is not proof either way. Only an oracle settles a finding.
- Never read real secrets into context. Compare by hash in-process, use fake canaries, and `stat` instead of `cat`.
- Severity reflects realistic preconditions. Raise it when an oracle shows real exposure; lower it when the oracle shows the precondition is heavy.

## Supporting files

- `templates/context.md`, `templates/ledger.md`, `templates/lane-brief.md`, `templates/refuter-brief.md`
- `templates/finding-schema.json`, `templates/verdict-schema.json`
- `gotchas.md`: tool and harness quirks (guards, ai-router, scanners, quota). Read it before phase 3.
