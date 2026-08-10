---
name: repo-autosync
description: Set up automatic git sync for a repo via a cron-driven Claude agent. Two modes — (1) push-local auto-commits + pushes local changes to origin; (2) upstream-merge keeps a fork up to date with upstream, vets changes via repo-check, merges, and resolves conflicts via a scoped agent. Use when the user asks to "keep a repo in sync automatically", "auto-commit changes", "keep fork up to date", "auto-merge upstream", "track upstream", or "set up auto-sync".
user-invocable: true
---

# Repo Auto-Sync Setup

Two modes:

- **push-local** — auto-commit local changes + push to origin (`sync-agent.sh`). For repos you edit, want committed cleanly.
- **upstream-merge** — for forks: fetch upstream, vet via `repo-check`, merge into fork, resolve conflicts via scoped agent, push to origin (`upstream-merge.sh` + `install-upstream-merge.sh`).

## Picking a mode

Ask the user:

1. Is the target a fork of someone else's repo that should track upstream?
   - Yes → upstream-merge
   - No → push-local

If upstream-merge, also ask:
   2. Pin to a specific upstream SHA (manual bumps only) or scheduled auto-update?
   3. If scheduled: cron expression (default: `0 */6 * * *`, every 6 hours)
   4. Merge target branch (default: parent's `default_branch` from GitHub API)

Confirm before proceeding.

## Prerequisites

Common:
- `claude` CLI on PATH
- `gitleaks` on PATH (push-local secret scan, upstream-merge alert on pre-existing secrets)

push-local only:
- Target repo on disk with remote origin

upstream-merge only:
- `gh` CLI authenticated (`gh auth status`) — for fork-parent detection
- `uv` on PATH — runs `repo-check`
- `~/Repos/repo-check/` cloned — upstream static-analysis + docker-sandboxed vet
- `jq` on PATH
- Repo has `upstream` remote (installer adds if missing, using `.parent.clone_url`)

## Mode 1: push-local

### Step 1: Confirm the target repo and schedule

Default schedule: daily at a random minute to avoid thundering herd.

### Step 2: Copy `sync-agent.sh` into the repo root

```bash
cp ~/.claude/skills/repo-autosync/sync-agent.sh /path/to/repo/sync-agent.sh
chmod +x /path/to/repo/sync-agent.sh
```

Do NOT inline or rewrite the script body — always copy from the canonical source so script improvements propagate on next copy.

### Step 3: Add a crontab entry

```
MINUTE HOUR * * * /path/to/repo/sync-agent.sh >> /path/to/repo/sync.log 2>&1
```

Use `crontab -e` or `(crontab -l; echo "...") | crontab -`. Add `CRON_TZ=Europe/Stockholm` at top if not present.

### Step 4: Add `sync.log` to `.gitignore`

### Step 5: Test

```bash
/path/to/repo/sync-agent.sh
```

### Guards and failure surfacing

`sync-agent.sh` fails closed and notifies on every abort path — cron has
nobody watching stdout, so a silent failure is indistinguishable from a
successful no-op. (Observed: aggregator failed 4 consecutive runs on a
Claude usage limit and went unnoticed for six days.)

Both guards scan only the files the run would actually commit — tracked
changes vs `HEAD` plus untracked-not-ignored — never the whole worktree.
Scanning the tree walks gitignored runtime dirs; on `~/.claude` that meant
hundreds of MB of session transcripts (>6 min, unfinished) versus 1.8s
scoped.

Abort paths, each emitting a stderr line plus a critical desktop
notification via `notify-send`:

| Guard | Trigger |
|-------|---------|
| Oversized file | any pending file > 5 MiB (`SYNC_MAX_FILE_BYTES` to override) |
| Secret scan | `gitleaks` missing, or a finding in a pending file |
| Agent failure | `claude` exits non-zero, **or** exits 0 having printed a usage-limit / API error |
| Incomplete sync | `claude` exits 0 but **tracked** changes are still uncommitted |
| Unpushed | commits made but `origin/$BRANCH` still behind |

The size guard is **per-file, not aggregate** — it cannot catch a thousand
small files, which is the actual shape of accidental runtime-state capture
(1496 files / 5.7 MB of reflection logs landed in `~/.claude` in one run,
every file a few lines). The ignore affordance below is the mitigation.

Claude Code prints `You've hit your org's monthly usage limit` and
`API Error: ...` to stdout and still **exits 0**, so the exit code alone
cannot distinguish an API failure from a finished sync. The agent's output is
captured and matched for those strings, then appended to whichever git-state
check actually failed. The text only ever *explains* a failure, never
declares one — a commit message containing "API Error" cannot fake a failure.

Only **tracked** changes count as incomplete. The agent is deliberately given
judgment over what belongs in the repo, and untracked leftovers are logged,
not escalated: aggregator's sync succeeded and pushed on every run for days
while reporting FAILED, because one ephemeral `mission.md.bak-*` sat
untracked and nothing would ever remove it — a permanently red guard that
trains you to ignore the notification.

### Agent affordances

The agent runs under `--allowedTools`, not `--dangerously-skip-permissions`:

```
Read  Bash(git status|diff|add|commit|push|log|rm|check-ignore:*)
Bash($REPO/.git/sync-agent-ignore:*)
```

`sync-agent-ignore` is generated into `$REPO/.git/` on every run (so
improvements propagate with the script, and it can never itself show up as a
pending change). It exists because the agent otherwise has *no* write
affordance: every generated file becomes a forced choice between committing
it and leaving the worktree dirty — and it commits.

```bash
$REPO/.git/sync-agent-ignore --reason "log output" 'logs/' '*.log'
```

Deliberately **not** the `Edit` tool: `--allowedTools` cannot path-scope
`Edit`/`Write` grants (probed 2026-08-01), so "let it edit `.gitignore`"
means unrestricted edit on an unattended cron agent. This command can only
append ignore rules to one file in one repo.

It fails the whole call — never a partial apply — on: negations (`!foo`,
which can expose files an existing rule deliberately hides), repo-swallowing
patterns (`*`, `**`, `/`, `.`), blank or newline-bearing patterns, and any
pattern matching an already-tracked file (ignoring a tracked file is a no-op;
git keeps committing it — the error names the files and points at
`git rm --cached`). Duplicates are skipped idempotently.

Set `SYNC_IGNORE_TARGET=exclude` to route rules to `.git/info/exclude`
instead of the tracked `.gitignore` — correct for public forks, where a local
automation rule must not leak into an upstream PR.

Success is silent and exits 0. The notifier writes its log line first so
failures stay diagnosable headless, then attempts the desktop notification
best-effort — a missing notification daemon must not become a second
failure. Cron inherits no session bus, so the script points
`DBUS_SESSION_BUS_ADDRESS` at `/run/user/$(id -u)/bus` when unset.

## Mode 2: upstream-merge

### Install

```bash
~/.claude/skills/repo-autosync/install-upstream-merge.sh \
  --repo /absolute/path/to/fork \
  [--schedule "0 */6 * * *" | --pin] \
  [--branch main]
```

Flags:
- `--repo` (required): absolute path to the fork
- `--schedule`: cron expression; omit for manual-only
- `--pin`: record current `upstream/BRANCH` SHA, no cron; re-run to bump
- `--branch`: merge target branch; defaults to parent's `default_branch` via `gh api`

What the installer does:
1. Verifies the repo is a git fork (`gh api repos/OWNER/REPO --jq .fork`). Aborts if not a fork.
2. Adds `upstream` remote if missing (parent's `.clone_url`). Warns if existing upstream points elsewhere.
3. Creates per-repo data dir: `~/.claude/repo-autosync-data/<repo-name>/`
4. Renders `upstream-resolve-settings.json` (scoped Claude for conflict resolve) into data dir
5. Writes `config.env` and `wrapper.sh` into data dir
6. If `--schedule`: adds cron entry tagged `<repo-name>-upstream-sync` (replaces existing tag idempotently)
7. If `--pin`: writes current upstream SHA to `<data>/pinned-sha`

### Pipeline (`upstream-merge.sh`)

1. `git fetch upstream`
2. No new commits → exit 0
3. `uv run python ~/Repos/repo-check/check.py <upstream-url> --output report.json` (all phases: A metadata+OSV, B SAST+plugin surface, C docker-sandboxed canary)
   - `verdict == reject` → write `.security-alert`, notify, exit (the `verdict_reason` field names which gate fired)
   - `verdict == approve` → continue
   - any other outcome (execution failure, unknown verdict) → fail closed, alert, exit
4. `git merge --ff-only upstream/$BRANCH` → push on success
5. Non-ff → `git merge upstream/$BRANCH` → push on success
6. Conflict → scoped agent (`Bash(git*)` + `Edit/Write` limited to `$REPO`) resolves and commits
7. Still conflicted after agent → `git merge --abort`, alert, exit
8. Clean → `git push origin $BRANCH`

**Security review is delegated entirely to repo-check.** No LLM review of diff content — that would be a prompt-injection surface (upstream code/comments could manipulate a reviewer agent). If repo-check's coverage is insufficient for a given class of attack, extend repo-check rather than adding a parallel LLM review.

### repo-check integration notes

- **All phases are mandatory.** The pipeline invokes `check.py <URL> --output report.json` with no disable flags. Phase A: OSV/CVE + GitHub metadata. Phase B: semgrep + gitleaks + symlink/archive safety + Claude-plugin surface (hooks, external MCP = instant reject, prompt-injection patterns, install scripts). Phase C: docker-sandboxed behavioral canary running 3 real incident scenarios (needs OAuth auto-found at `~/.claude/.credentials.json` + prebuilt docker image).
- repo-check is under active development. Our integration parses `.verdict` and `.verdict_reason`. The verdict is binary (`approve` | `reject`), driven only by deterministic gates — the prior `review` middle band was removed because gating on a continuous score was symmetric-flaky (false-rejected legitimate plugins on hook count; false-approved popular-but-compromised repos that scored well on metadata). The report also carries a `.risk_advisory` field (score + per-axis components: cves, semgrep, plugin_surface, plugin_injection, metadata) for the manual-CLI path; autosync ignores it on purpose. Re-test after repo-check releases.
- **Full-scan, not diff.** Every run vets upstream HEAD in full. Correct for "is landing state safe" but a CVE in an existing dep blocks every run until it is resolved (or repo-check gains `--upstream-base SHA`).
- **Fail-closed on:** clone failure, execution error, unknown verdict, `phase_b.aborted`, Phase C error, Phase C triggered, any deterministic gate. Verdict `reject` stops the merge.

### Pin mode

No cron. Installer records current `upstream/$BRANCH` SHA to `<data>/pinned-sha`. Re-run installer (same flags) to bump the pin after manually verifying changes.

### Data layout per repo

```
~/.claude/repo-autosync-data/<repo-name>/
├── config.env                           # REPO, UPSTREAM_URL, BRANCH, etc.
├── wrapper.sh                           # sources config.env, execs canonical script
├── upstream-resolve-settings.json       # scoped Claude for conflict resolve
├── last-update.log                      # append-only log
├── repo-check-report.json               # last vet result
├── .security-alert                      # present when blocked (absent = healthy)
├── pending_for_human.md                 # appended on no-op-but-stale repos / unknown failure modes
└── pinned-sha                           # present in pin mode only
```

### Health checks

- `.security-alert` present → something was blocked; read `last-update.log` + `repo-check-report.json`
- `pending_for_human.md` present → manual review queued
- Absent → healthy

## Canonical files

Keep scripts in skill dir; installer pre-renders per-repo settings into the data dir. Upgrading the canonical scripts propagates on next cron run without re-install.

- `sync-agent.sh` — push-local pipeline
- `upstream-merge.sh` — upstream-merge pipeline (canonical, parameterized via env)
- `upstream-resolve-settings.json.tmpl` — scoped Claude config template
- `install-upstream-merge.sh` — per-repo installer

## Notes

- Neither mode uses `--dangerously-skip-permissions`. push-local passes an explicit `--allowedTools` allowlist (see Agent affordances above); upstream-merge uses explicit settings files (scoped deny-lists).
- Symlink propagation works regardless of mode: if the repo is symlinked into `~/.claude/skills/<name>`, merges land live after `git push origin` returns (the symlink target IS the working tree).
