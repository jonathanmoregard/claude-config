---
name: handling-dependency-advisories
description: Use when a gate, CI job or check fails on a dependency security advisory unrelated to the current task — cargo-deny or cargo audit (RUSTSEC, `advisories FAILED`, `error[vulnerability]`, `error[unsound]`, "security vulnerability detected"), npm audit, pip-audit, or a Dependabot alert.
---

# Dependency advisories

Standing user answer. Do not ask.

Fix it in this branch in an isolated commit: the smallest semver-compatible
bump (`cargo update -p <crate> --precise <patched>`, `npm audit fix` without
`--force`), dependency files only. Then send off a subagent to cherry-pick
that commit into a separate PR off the freshly fetched default branch, to
handle it asap. Tell the subagent the PR is pre-authorized, also in PR-gated
repos, and to run `gh pr create` without `--base`.

No patched version: change nothing, report it. Never add an ignore or
exception without the user's consent.
