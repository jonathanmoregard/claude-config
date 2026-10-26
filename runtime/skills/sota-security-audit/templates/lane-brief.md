# Fix-lane brief (shared by all lanes; read in full)

1. Read in full and treat as binding: the user's global CLAUDE.md and `<notes>/session-constraints.md`. Read them yourself; the orchestrator must not paraphrase them. That covers PR count, merge rules, commit trailers, and worktree rules.
2. Your items are listed under your lane in `<notes>/<audit>/fix-plan.md`. The evidence for each one (finding, refuter verdict, oracle) is in `<notes>/<audit>/review/`. Read each refuter verdict: it often names a cheaper or more correct fix, or a wrong citation.
3. Per item:
   - Reproduce it with an oracle or a RED test.
   - Make the minimal fix.
   - Get it GREEN.
   - Run a variant sweep (ripgrep → semgrep) for the same root cause.
   - Measure the capability cost against real usage (history, logs, CI) and state it.
   If an item is wrong, can't be fixed cheaply, or needs a user decision, skip it and say why. Don't widen scope.
4. Never print secrets. Never call third-party APIs except your own git host for push/PR. Never edit live config checkouts that are generated or pinned.
5. Run the fastest relevant check after every edit, and the repo's full gate before the PR. Open ONE PR per repo.
6. Write per-item status to `<notes>/<audit>/lane-<name>.json` as `[{id, status, evidence, cost}]`, then return ≤25 lines: items, evidence, PR URL, and decisions needed.
