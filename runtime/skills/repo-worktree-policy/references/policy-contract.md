# Policy contract

## Source of truth

Canonical implementation is Klaffat's tracked policy bundle under
`~/Repos/klaffat`. It is a selective port of mature `.claude` worktree-lock
work: Git-native guard, global-hook forwarding, installer, worktree helper,
session self-heal, strict shared puller seams, and behavioral tests.

Copy that implementation. Do not recreate its ref-transaction logic from
memory and do not port `.claude`'s large command parser.

## Required invariants

- Scope every guard by remote named `origin` with exact tail
  `owner/repo(.git)`, not directory name or any matching secondary remote.
- Detect primary structurally: absolute Git dir equals absolute common dir.
- Protect only primary checkout. Linked worktrees remain normal development
  environments.
- Primary `HEAD` stays attached to declared default branch.
- Default branch may advance only by fast-forward. Allow no-op ref updates,
  worktree branch creation, feature deletion, `pack-refs`, and `git gc`.
- Reject commits in primary using `prepare-commit-msg`; it still runs with
  `git commit --no-verify`.
- Store absolute primary `.githooks` in shared `core.hooksPath`.
- Refuse to replace an unrelated repository-local `core.hooksPath`; those hooks
  need explicit migration.
- Preserve every executable global hook, its arguments/stdin, and exit status.
  Missing forwarders must self-heal without wedging future pulls.
- Worktree helper fetches declared upstream branch, creates a fresh owned
  branch/path, refuses invalid refs and collisions, and self-heals hook
  activation before touching refs.
- Session observer is silent when healthy, repairs hook activation, reports
  drift, always exits zero, and never mutates dirty/off-branch user work.
- Puller checks expected branch, exact `origin/<branch>` upstream, and full
  tracked/untracked cleanliness before fetch. Update is fetch plus
  `merge --ff-only` only. Each repository gets a separate service/timer
  failure domain and names that unit in failure guidance.
- Primary repair belongs to repository owner. Agent stops and reports rather
  than stashing, resetting, cleaning, or switching unknown work.

## Required behavior tests

At minimum prove:

- installer rejects wrong `origin`, refuses unrelated repository-local hooks,
  and anchors absolute hooks path on primary;
- primary cannot leave default branch or commit, including `--no-verify`;
- primary accepts fast-forward update and rejects non-fast-forward movement;
- helper rejects invalid/colliding names, creates expected worktree/branch,
  and activates hooks;
- linked worktree commits normally;
- unrelated repositories remain unaffected;
- global pre-commit/pre-push/prepare/reference hooks remain chained;
- session observer self-heals and healthy run is silent;
- strict pull rejects wrong/detached branch, wrong upstream, and
  tracked/untracked dirt before fetch, while defaults preserve existing puller
  behavior;
- scheduler renders absolute repo, branch, clean guard, persistent timer, and
  expected cadence; matching Nix VM lane passes.

## Avoided designs

- No global repo registry or hook multiplexer.
- No duplicate pull daemon or repository-specific fork of shared puller.
- No relative hooks path.
- No path-only scoping.
- No blanket ref-create/delete block that breaks worktrees or Git maintenance.
- No destructive automatic primary repair.
- No claim that hooks prevent a deliberate `core.hooksPath=/dev/null` bypass.

## Merge order

When shared puller seams, repository policy, and Nix timer are separate PRs:

1. shared puller/config skill PR;
2. repository policy PR;
3. NixOS service/timer PR.

After repository policy lands, run its installer once or let first project
session/worktree-helper self-heal. NixOS deployment then starts guarded pulls.
