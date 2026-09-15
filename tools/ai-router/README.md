# ai-router

Cross-provider subagent routing between Claude Code and Codex CLI. Picks a
model from the operator-pinned ranking in `models.toml`, filtered by live
usage-cap headroom, and runs headless jobs detached from the calling session.

Launcher: `~/.claude/bin/ai-router` (absolute path works from either host CLI).
Implementation: `tools/ai-router/ai_router/`, stdlib only, Python 3.14.

## Commands

Every subcommand takes `--json` (one object on stdout); otherwise one short
human line.

| Command | What it does |
|---|---|
| `quota [--fresh] [--provider codex\|claude]` | Normalised usage snapshots per provider (buckets, headroom, staleness, rejection). |
| `pick --tier strongest\|standard\|fast [--need CAP …] [--provider P] [--host H] [--allow-downgrade] [--fresh]` | Choose a model. `native: true` means the picked provider is the host CLI: use its own subagent tool. |
| `delegate (--tier T \| --model M) --prompt-file F [--cwd D] [--sandbox read-only\|workspace-write] [--schema S.json] [--timeout-s N] [--effort E] [--wait] [--force] [--no-bare] [--host H] [--name NAME] [--allow-downgrade]` | Start a headless job (always headless, even on the host provider). `--model` keeps the eligibility check unless `--force`. |
| `wait <job_id> [--timeout-s N]` | Block until terminal, following an automatic retry; prints the result. |
| `status [<job_id>]` · `list [--all]` | One job or all jobs (newest first, 20 by default). |
| `peek <job_id>` · `log <job_id> [--tail N]` · `result <job_id>` | Newest agent message · summarised events + stderr · final result (exit 7 while running). |
| `send <job_id> --prompt-file F [--wait]` | Post-hoc follow-up: `codex exec resume <thread>` / `claude -p --resume <session>`. |
| `cancel <job_id>` · `version` | |

Quota failure handling: a job that fails on a quota class writes
`quota/<provider>.rejected.json` (until the blocking bucket resets, or a newer
live snapshot shows headroom again) and is retried exactly once on the next eligible model of the same tier
(`retry_of` / `retried_as` in `meta.json`; `wait` follows the chain).

Claude jobs never pass `--bare`: on 2.1.270 `--bare` refuses OAuth
credentials, which is how this machine authenticates. "Bare" is spelled
`--setting-sources "" --strict-mcp-config` (no hooks, no MCP, no user
permission rules); `--no-bare` loads settings normally.

## Selection

`pick` scores every model of the requested tier against its provider's
usage-cap snapshot. Per applicable bucket (window `window_min`, reset
`resets_at`, `used_pct`), at time `now`:

```
left     = clamp((resets_at − now) / window, 0, 1)   # fraction of the window ahead
line     = 100 · left                                # where linear pacing would leave it
headroom = 100 − used_pct
surplus  = headroom − line                           # pct points ahead (+) / behind (−) pace
floor    = min(reserve_pct, line)                    # reserve that descends with the line
```

Per model: `headroom` and `surplus` are the minimum over its counted buckets
(applicable, not resetting within `near_reset_relax_min`; `surplus` is null
when no counted bucket carries reset geometry). A model is **eligible** when
its provider is not rejected, every counted bucket has `headroom ≥ floor`,
the model has every `--need` capability and (Codex) appears in a fresh
`model/list`. The floor holds at `reserve_pct` for most of the window and
slides to 0 over the final stretch, so a period can end near 0 without
running dry early.

`settings.policy` orders the eligible models of the first tier that has any:

| policy | choice |
|---|---|
| `pace` (default) | pins order among providers on pace (`surplus ≥ −pace_slack_pct`, or unknown); when every provider is behind, the least behind; ties keep pins order |
| `rank-first` | first eligible in pins order, headroom ignored |
| `headroom-weighted` | largest raw headroom, ties keep pins order |

`candidates[].why` spells the verdict ("headroom 39 %, 14 pts behind pace";
"headroom 10 % < reserve floor 15 %"); the human line prints `pace=+7.3`.

## State directory

`$AI_ROUTER_STATE`, default `~/.local/state/ai-router/` (mode 0700):

- `quota/codex.json`, `quota/claude.json` — probe snapshots
  (`claude.json` is written raw by the statusline tee hook, or by the OAuth
  fallback when the tee is stale).
- `quota/<provider>.rejected.json` — provider rejected until `until`, or
  until a newer snapshot shows every applicable bucket with headroom ≥ reserve
  (the marker is then deleted: the window rolled or the parsed reset was wrong).
- `quota/claude.oauth-backoff` — epoch; OAuth probe skipped until then.
- `jobs/<job_id>/` — `meta.json`, `prompt.md`, `events.jsonl`, `stderr.log`,
  `status`, `exit_code`, `result.txt`, `result.json` (when parseable),
  `last_message.md` (codex `-o`), `runner.log` (setsid launches only).

All writes are atomic (`<file>.tmp` then rename).

## Exit codes

`0` ok · `1` job error / cancelled · `2` usage or setup (missing binary, bad
args) · `3` quota (no eligible model, or job failed on quota after its retry)
· `4` rejected / unavailable · `5` stalled / timeout / overloaded · `6`
unparseable · `7` job not found / not finished.

## Test-only environment

`AI_ROUTER_CODEX_BIN` / `AI_ROUTER_CLAUDE_BIN` override binary resolution;
the unit suite points them at `tests/fixtures/fake_codex.py` and
`fake_claude.py`. No test performs a network or real provider call.

## Running the tests

pytest is not in the base `python3`; use the nix-provided one:

```
nix shell nixpkgs#python3Packages.pytest -c pytest -q tools/ai-router/tests
```
