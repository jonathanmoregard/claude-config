---
name: agent-config-e2e
description: Use before opening a PR for any behavior-changing agent config edit — CLAUDE.md, a skill, an agent, a command, a hook, settings.json, permissions, or any text an agent reads — and when asked "did you test it e2e?", "does the agent actually do this now?", "prove the rule works", or to e2e-test a config change. The agent-config counterpart of nixos-agent-testing.
---

# Agent-config e2e

A config change is a claim about what agents will DO. Unit tests prove a
script's branches; reading the diff proves the text says something. Neither
shows an agent, loaded with the new config, behaving differently in the
situation the change targets. This skill does: like `nix run .#feature-vm`
for nixos-config, it installs the candidate config into a throwaway HOME and
drives a real headless agent through the scenario.

**Don't grep the text and call it tested. Run the agent through the
situation.**

## When

Every behavior-changing edit to `~/.claude`: CLAUDE.md, skills, agents,
commands, hooks, settings (permissions, defaultMode, env), templates agents
read. A hook-script change also needs its unit tests; e2e shows the hook fires
inside a real session and the agent then acts on it.

Skip only when no agent's behavior can change: a typo in a comment, a
non-agent script with its own tests, a formatting-only diff.

## Quick start

```bash
cd ~/worktrees/claude-<slug>
git add -A && git commit -m wip   # arm is a ref; -A so new skills/scenarios are in it
python3 scripts/agent-e2e/agent_e2e.py run tests/agent-e2e/<scenario> \
  --arm old=origin/master --arm new=$(git rev-parse --short HEAD) --n 5
```

Run it with `run_in_background: true`, since a batch takes minutes. Finished
runs are skipped on re-run, so a killed batch never re-spends.
`agent_e2e.py --help` has the full contract. `stage --config SRC --home DIR`
builds one sandbox HOME so you can poke it by hand.

## Writing a scenario

`tests/agent-e2e/<name>/` holds three files:

| File | Holds |
|---|---|
| `prompt.md` | The user's message, verbatim. Phrase it as the user would, with no hint of the rule under test |
| `setup.sh` | Builds the workspace in `$E2E_WORK`. Puts stubs in `$E2E_BIN`, which is first on PATH, so `gh`, deploys and paid APIs never touch the real world. Logs go to `$E2E_RUN` |
| `check.sh` | Grades one run from `final.txt`, `commands.txt` (every Bash command, in order), `transcript.jsonl` and the stub logs. Exit 0 PASS, 1 FAIL, else INCONCLUSIVE |

`tests/agent-e2e/_lib/` has a GitHub-looking repo with finished work on a
pushed branch (`repo.sh PRIVATE|PUBLIC`), a logging `gh` stub, and graders
(`pr_attempted`, `asks_about_pr`).

Cover each branch the change creates, plus a control showing the old,
still-wanted behavior survives. The PR-gate set is the example: unreviewed
(hold), reviewed (ask), explicit request (create), public (create as before).

Grade **actions** over prose. A stub's log, a command issued, or a file written
beats a regex over the final message. Use text graders only for what is only
text, such as "asked a question".

## Reading results

- **Probe first.** Run `--n 1` on the new arm and read the transcript yourself
  before the batch. The first probe of this rig "passed" because a hook
  refused every command, so the agent could not have opened a PR anyway. The
  harness now marks that INCONCLUSIVE, but other sandbox faults look like
  passes too.
- INCONCLUSIVE runs are reported, excluded from the rate, and never retried
  into replacements.
- n=5 per arm shows large effects only (≈ 0/5 vs 5/5). Say so. A null is a
  result. For a causal "this edit fixes that past failure" claim, use
  `ab-testing-prompt-changes`, which adds contamination checks and
  pre-registration.
- Paste `summary.md` into the PR body under **Agent e2e**, with the
  transcripts' path.

## Fidelity limits

- Headless `claude -p`, not interactive. Anything that would prompt is
  denied instead. `defaultMode`, hooks and permissions come from the staged
  config.
- No MCP servers (`--strict-mcp-config`). Credentials, the plugin cache,
  `~/Repos` and caveman config are linked from the real HOME. dcg's config is
  copied.
- Isolation needs bubblewrap: a private `/tmp`, the real HOME read-only,
  `~/.ssh` hidden, and credential env vars (`SSH_AUTH_SOCK`, `GH_TOKEN`, …)
  scrubbed. Without `bwrap`, verdicts say `isolated_tmp: false`. Don't run
  concurrent batches that way.
- Workspaces live under `/tmp`, where `finishing-up-arm.sh` deliberately
  arms nothing, so the DoD informer never fires in e2e. A change to it needs
  its own scenario rooted outside `/tmp`.
- One user turn. A multi-turn history is described in the prompt, not
  replayed.
- Runs draw on the subscription quota, about $0.25–1 equivalent per run.
  `--budget` is a per-run truncation guard, not a bill.
