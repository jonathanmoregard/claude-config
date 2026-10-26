---
name: repo-worktree-policy
description: Use when asked to keep a repository's primary checkout on its default branch, require development in linked worktrees, protect the primary checkout from agent edits, or add scheduled fast-forward updates.
---

# Repository Worktree Policy

Turn one checkout into a stable integration/pull target while all development
happens in owned linked worktrees. Reuse Klaffat's tested implementation; do
not design another hook or sync framework.

## Required workflow

1. Read `references/policy-contract.md` completely.
2. Invoke `using-git-worktrees` and `test-driven-development`. For NixOS
   scheduler changes, also invoke `nixos-config-dev` and
   `nixos-automated-testing`.
3. Inspect repository instructions, exact `origin` owner/repo, upstream default
   branch, primary checkout path, existing worktrees, dirt, hooks, and update
   automation. Never infer default branch from a generic `main`/`master` habit.
4. If primary checkout is dirty or off its default branch, stop mutation and
   report exact state. Never stash, reset, clean, switch, or move unknown work.
5. Create task-owned worktrees for every changed repository. Config changes to
   `~/.claude` use its required config-worktree/PR flow; NixOS changes use
   `~/Repos/nixos-config-worktrees/<slug>`.
6. Copy Klaffat's policy bundle from `~/Repos/klaffat`, then adapt only names,
   exact remote slug, default branch, paths, and test fixtures:

   ```text
   .githooks/reference-transaction
   .githooks/_forward-global-hook
   .githooks/pre-commit
   .githooks/pre-push
   .githooks/prepare-commit-msg
   scripts/install-worktree-policy.sh
   scripts/klaffat-worktree.sh
   scripts/worktree-policy-session-start.sh
   tests/worktree-policy.sh
   ```

   Rename repository-specific helpers. Keep scripts executable. Use patch-based
   edits so substitutions remain reviewable.
7. Add first-screen instructions to both `CLAUDE.md` and `AGENTS.md`: primary
   path/default branch, forbidden mutations, exact helper command, worktree
   destination, and stop/report behavior on drift.
8. Register session observer in project `.claude/settings.json`. This gives
   Claude self-healing/diagnostics; `AGENTS.md` and Git hooks cover Codex and
   other clients.
9. Reuse `~/.claude/scripts/claude-pull.sh` for scheduled pulls. Set absolute
   `CLAUDE_PULL_REPO`, `CLAUDE_PULL_EXPECT_BRANCH`,
   `CLAUDE_PULL_EXPECT_UPSTREAM=origin/<branch>`,
   `CLAUDE_PULL_REQUIRE_CLEAN=1`, and the dedicated `CLAUDE_PULL_UNIT` in a
   systemd user service. Never add reset, stash, normal merge, conflict
   resolution, or a second pull script.
10. Test red first, then green. Run policy behavior tests, project checks,
    `.claude` pull tests, relevant Nix VM lane, `git diff --check`, and
    `verification-before-completion`. Use `advice-refine-test-loop` before push.
11. Commit and open dependent PRs. Do not activate tracked hooks from an
    unmerged branch: after repository PR lands, installer/session observer
    anchors `core.hooksPath` to primary checkout. State safe merge order.

## Output

Report protected checkout, worktree command/root, pull cadence, enforcement
layers, tests, PRs, merge order, and any residual bypass boundary. Git hooks
are guardrails, not a hostile security boundary: explicit hook disabling can
bypass them.
