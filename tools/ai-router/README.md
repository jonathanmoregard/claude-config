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
`quota/<provider>.rejected.json` (until the blocking bucket resets) and is
retried exactly once on the next eligible model of the same tier
(`retry_of` / `retried_as` in `meta.json`; `wait` follows the chain).

Claude jobs never pass `--bare`: on 2.1.270 `--bare` refuses OAuth
credentials, which is how this machine authenticates. "Bare" is spelled
`--setting-sources "" --strict-mcp-config` (no hooks, no MCP, no user
permission rules); `--no-bare` loads settings normally.

## State directory

`$AI_ROUTER_STATE`, default `~/.local/state/ai-router/` (mode 0700):

- `quota/codex.json`, `quota/claude.json` — probe snapshots
  (`claude.json` is written raw by the statusline tee hook, or by the OAuth
  fallback when the tee is stale).
- `quota/<provider>.rejected.json` — provider rejected until `until`.
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
