---
name: ai-router
description: Use when choosing between Claude and Codex providers, delegating work across providers (including from a sandboxed Codex host), running Klaffat gates from Codex, handling a usage cap or rate limit ("You've hit your", "usage limit", weekly limit, 5-hour limit), checking quota headroom, load-spreading between the ChatGPT Pro and Claude Max plans, or running a job on gpt-5.6-sol/terra/luna from Claude. Native Claude-to-Claude subagents use the pinned role agents instead.
---

# ai-router — cross-provider subagent routing

Claude Code uses the absolute CLI path:

```
/home/jonathan/.claude/bin/ai-router <subcommand> --json
```

It picks a model by the operator-pinned ranking in
`/home/jonathan/.claude/tools/ai-router/models.toml` AND live usage-cap
headroom per provider (Codex via `codex app-server`, Claude via the statusline
tee), then runs cross-provider work as a detached job you can wait on, peek
into, and resume. Same-provider work stays native (see "Native vs delegate").

Normal Claude-to-Claude routing does not use this router: `Explore` is Haiku,
`worker-easy` is Sonnet, `worker-hard` is Opus, and `advisor` is Fable. Their
full model IDs are pinned in `~/.claude/agents/`; do not override them per
call. Use ai-router when provider selection or quota escape is actually part
of the task.

Sandboxed Codex sessions use the host-side `mcp__ai_router__ai_router` tool,
not the CLI directly. A nested `codex exec` started inside the managed Codex
sandbox cannot initialize or reach the provider. The MCP server runs on the
host, writes the supplied prompt to the job prompt file through the real CLI,
and returns a detached job ID. It exposes only `delegate`, `status`, bounded
`wait`, `result`, and bounded `log`; it never exposes `--force`,
`--allow-downgrade`, raw flags, shell text, or environment overrides.

Pins (2026-09-14): `gpt-5.6-sol` > `claude-fable-5-1` > `claude-opus-5` >
`gpt-5.6-terra` > `claude-sonnet-5` > `gpt-5.6-luna` >
`claude-haiku-4-5-20251001`. Tiers: strongest = sol/fable/opus, standard =
terra/sonnet, fast = luna/haiku. A provider is skipped when any of its
usage buckets sits under its floor — the 15 % reserve for most of the
window, descending to 0 over the final stretch before that bucket resets —
or when it has been rejected by a quota exhaustion; a rejection
lapses at its reset time or as soon as a newer live snapshot shows every
bucket above its floor again. Among eligible providers `pick` paces:
`surplus` is how many points a provider sits ahead (+) or behind (−) the
linear spend line to its reset; the top pin wins while on pace, otherwise
the provider with the most slack. Unknown quota is reported as unknown,
never guessed.

## Decide: native or delegate

1. `ai-router pick --tier <strongest|standard|fast> --json` (add `--need
   vision|write|long-context` to constrain). Run it unconstrained first: it
   already paces both providers against their reset windows (`surplus` in
   the output), so never add `--provider` to second-guess headroom or
   quota — only when the task itself needs one host (shared worktree, peek
   into the subagent, same-context resume).
2. Read `native`:
   - `native: true` → the pick is your own provider. On Claude, dispatch the
     appropriate named role (`Explore`, `worker-easy`, `worker-hard`, or
     `advisor`) without a per-call model override. Codex never spawns natively
     — see below.
   - `native: false` → cross-provider. Use `delegate` (below).
   - `ok: false` → nothing eligible in that tier; add `--allow-downgrade` or
     wait for the reset shown in `candidates[].why`.
Codex hosts: Codex→Codex is NEVER native (subagent model pinning is
unreliable there). Call the structured host MCP `delegate` operation with an
explicit `model` (or a `tier`) so the new process is pinned; the broker always
passes `--host codex` itself.

## Delegate a job (cross-provider)

From Claude, write the task to a file (prompt = the whole file, verbatim), then:

```
/home/jonathan/.claude/bin/ai-router delegate --tier standard \
  --prompt-file /abs/path/task.md --cwd /abs/repo \
  --sandbox read-only --json
```

- `--tier` picks by ranking + headroom and may retry once on another model
  after quota exhaustion. `--model <id|alias>` pins the exact model/provider:
  quota, capacity, or rate-limit failures never switch it (eligibility still
  applies; `--force` overrides eligibility and is recorded in the job).
- `--sandbox read-only` (default) or `workspace-write`. Codex gets its OS
  sandbox; Claude gets `--permission-mode default --tools Read,Glob,Grep`
  for read-only and `acceptEdits` + file/Bash tools for workspace-write.
  There is no flag that bypasses either sandbox.
- This host uses managed permission profiles. Under them Codex ignores
  `--sandbox`, `--add-dir`, and
  `sandbox_workspace_write.network_access=true`. Read-only delegates therefore
  pass `-c default_permissions="repos_readonly"` explicitly (including resume),
  while workspace-write uses the host's `repos_dev` profile. `meta.json`
  records the effective managed profile and states that CLI overrides are
  inert.
- `--dev-gates` is rejected because it cannot make Nix or network available
  under `repos_dev`. From Codex, run Klaffat gates through
  `mcp__klaffat-app-runner__klaffat_app` with `operation=start`, `mode=check`,
  the linked `worktree_path`, and one of `check_kind=affected|focused|full|rust_test`.
  Poll `status` with `wait_s<=600`, then inspect bounded `logs`. `focused`
  also requires `focused_target`: an existing `tests/e2e/tests/*.spec.ts`, a
  workspace Cargo package, or a `scripts/test-lanes.json` focused-test ID.
  `rust_test` requires `crate` and optionally `filter`.
- `--schema schema.json` forces structured output (Codex `--output-schema`,
  Claude `--json-schema`); `result` then carries `result_json`.
- `--effort low|medium|high|xhigh|max` (Codex also `ultra`). `--need
  vision|write|long-context` constrains a `--tier` pick.
- Claude jobs run hermetic by default: no settings files, hooks or MCP
  (`--setting-sources "" --strict-mcp-config`; Claude's own `--bare` is not
  used because on 2.1.270 it refuses OAuth credentials). `--no-bare` loads
  settings, hooks and CLAUDE.md normally.
- `--wait` blocks until the job is terminal and prints the result. Without
  it the command returns `{job_id, job_dir, events, result_path, …}` at once.

Jobs are detached (`systemd-run --user`, fallback `setsid`), so they survive
your tool call ending. Job state: `~/.local/state/ai-router/jobs/<job_id>/`
(`meta.json`, `events.jsonl`, `stderr.log`, `status`, `result.txt`,
`result.json`).

### Follow a running job

```
ai-router status <job_id>          # queued|running|done|failed:<class>|stalled|timeout|cancelled
ai-router peek <job_id>            # newest agent message (sanitised)
ai-router log <job_id> --tail 50   # event summary + stderr
ai-router wait <job_id> --timeout-s 900
ai-router result <job_id>          # result_text, result_json, usage, error_class
ai-router send <job_id> --prompt-file follow-up.md   # post-hoc resume (new job, parent_job set)
ai-router cancel <job_id>
ai-router list
```

In Claude Code the ergonomic pattern is `Bash(run_in_background=true)` on
`ai-router wait <job_id>` and reading the notification, or `--wait` with a
Bash timeout ≤ 10 min. Longer jobs: poll `status` from a `/loop` or
`ScheduleWakeup`.

### What you lose versus a native subagent

- No mid-flight `SendMessage`; `send` resumes the finished thread instead.
- Not in the agents panel; it is a background process with files.
- No shared permission prompts: the job runs with the sandbox you chose and
  auto-denies anything outside it (check `stderr.log` when a job ends
  `failed:error` quickly).

## Quota

```
ai-router quota --json            # both providers, buckets, headroom, rejected_until
ai-router quota --fresh           # bypass the 60 s Codex cache
```

Failure classes on jobs: `quota` (provider marked rejected until its reset;
tier-picked jobs retry ONCE and record `retried_as`, while explicit `--model`
jobs keep that model and record a retry note), `rate_limit` (terminal for this
attempt; provider stays eligible and no cross-model retry), `rejected`
(model/request refused — deterministic, not retried), `stalled` (no output
for 10 min), `timeout`, `unparseable`, `error`. Exit codes: 0 ok · 1 job failed
(error) or cancelled · 2 usage/setup · 3 quota · 4 rejected · 5
rate-limit/stalled/timeout · 6 unparseable · 7 job not found/not finished.

## Ranking evidence

`python3 /home/jonathan/.claude/tools/ai-router/update_ranking.py` (daily
systemd user timer) refreshes `~/.local/state/ai-router/ranking.auto.json`
from LMArena and Epoch AI and writes `ranking.proposal.md` plus a
`pending_for_human.md` entry when the evidence disagrees with the pins. It
never reorders the pins; changing them is an edit to `models.toml` through the
`~/.claude` worktree + PR flow.

## Editing this tool

`~/.claude` is read-only: changes to `bin/ai-router`, `tools/ai-router/**`,
`hooks/ai-router-*`, `hooks/statusline-quota-tee.sh` or this skill go through
`~/worktrees/claude-<slug>/` off fresh `origin/master`, then a PR. Tests
(pytest is nix-provided, not in the base python):
`nix shell nixpkgs#python3Packages.pytest -c pytest -q tools/ai-router/tests tests/test_ai_router_hooks.py tests/test_ai_router_health.py tests/test_native_agent_routing.py`.
