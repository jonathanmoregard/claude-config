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

Commit the change and its scenario (`git add -A`, so new files are in it),
push the branch, then call the MCP tool `mcp__agent_e2e_broker__run`:

```
{"scenario": "<scenario>", "old_ref": "origin/master",
 "new_ref": "origin/feat/<slug>", "model": "haiku", "n": 2}
```

It runs origin/master's harness outside the Bash sandbox, with the scenario
from `new_ref`, and returns the summary table, every verdict and the
transcripts' directory. It blocks until done. A few campaigns run at once; when
every slot is busy the call waits up to 30 minutes for one before it errors.
`model` is haiku or sonnet and `n` at most 2 per call, so call it again for
more samples and add the counts. Calls in one message run one after another; for
parallel campaigns use separate sessions or subagents, since each call blocks its caller.

Run the harness directly only when the broker cannot serve: a change to the
harness itself, an unpushed ref, or a larger batch. It cannot run inside the
Bash sandbox.

```bash
cd ~/worktrees/claude-<slug>
git add -A && git commit -m wip
python3 runtime/scripts/agent-e2e/agent_e2e.py run tests/agent-e2e/<scenario> \
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
| `check.sh` | Grades one run from `final.txt`, `commands.txt` (every Bash command, in order), `transcript.jsonl` and the stub logs. Exit 0 PASS, 1 FAIL, else INCONCLUSIVE. Grade the agent's free text (final message, files it wrote) through a schema-enforced judge, never by grep or regex: **REQUIRED SUB-SKILL:** `parsing-subagent-output` |

`tests/agent-e2e/_lib/` has a GitHub-looking repo with finished work on a
pushed branch (`repo.sh PRIVATE|PUBLIC`), a logging `gh` stub, and graders
(`pr_attempted`, `asks_about_pr`).

Cover each branch the change creates, plus a control showing the old,
still-wanted behavior survives. The PR-gate set is the example: unreviewed
(hold), reviewed (ask), explicit request (create), public (create as before).

Grade **actions** over prose. A stub's log, a command issued, or a file written
beats any reading of the final message. What is only text, such as "asked a
question", goes through the schema-enforced judge
(`_lib/structured_judge.py`). `asks_about_pr` is an older regex grader: don't
use it in new scenarios.

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
- Paste the summary table (`summary.md`) into the PR body under
  **Agent e2e**, with the transcripts' path.

## Fidelity limits

- Headless `claude -p`, not interactive. Anything that would prompt is
  denied instead. `defaultMode`, hooks and permissions come from the staged
  config.
- No MCP servers (`--strict-mcp-config`). Credentials, the plugin cache,
  `~/Repos`, `~/worktrees` and caveman config are lent from the real HOME
  (read-only, credentials writable). dcg's config is copied.
- Isolation needs bubblewrap: a private `/tmp`, the staged HOME mounted at
  the real HOME's path with nothing else of the real one visible (`~/.ssh`
  empty), and credential env vars (`SSH_AUTH_SOCK`, `GH_TOKEN`, …)
  scrubbed. Without `bwrap`, verdicts say `isolated_tmp: false`. Don't run
  concurrent batches that way.
- One user turn. A multi-turn history is described in the prompt, not
  replayed.
- Sessions run with `--no-session-persistence`, so a hook reading
  `transcript_path` sees no file. A scenario testing such a hook adds an
  empty `persist-session` file to its directory.
- Runs draw on the subscription quota, about $0.25–1 equivalent per run.
  `--budget` is a per-run truncation guard, not a bill.
