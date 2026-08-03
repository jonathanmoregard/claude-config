# Permission Friction Ledger

A record of every permission prompt Claude Code shows you, and a nightly agent
that turns the recurring ones into reviewable proposals.

Permission prompts get approved reflexively, and nothing records them, so there
is no way to tell which ones guard something real and which are pure
configuration friction. Transcripts do not contain permission events at all —
only tool calls that already succeeded — so this has to be captured live.

**Propose-only.** Nothing here ever edits `settings.json`. Every change goes
through you, in `/review-improvements`.

## The three stages

1. **Capture** — `hook.py`, registered on the `PermissionRequest` and
   `PermissionDenied` hook events with matcher `*` and `timeout: 5`. Fires when
   a prompt is about to be shown (no allow/deny rule matched) and when you
   decline. Appends one JSON line per event to a monthly ledger file. Never
   blocks, never decides, always exits 0 — a ledger fault must not change a
   permission outcome. Subagent-originated prompts are captured too, because
   `agent_id`/`agent_type` are in the hook payload's base fields.

   Registered as `python3 -S -E $HOME/.claude/permission-ledger/hook.py`. Of
   the ~150 ms a permission dialog costs, ~80 ms is bare interpreter startup;
   `-S` skips `site` processing and `-E` ignores `PYTHONPATH`, which is free
   and safe because `hook.py` is stdlib-only. Both the installer's idempotency
   check and the uninstaller's matcher key on the `permission-ledger/hook.py`
   substring, never the whole command, so changing these flags again will not
   orphan the old entry.

2. **Aggregate** — `aggregate.py`, deterministic and model-free. Reads the last
   7 days, joins denials onto requests, groups by `pattern_key`, drops groups
   under 3 occurrences, drops groups already decided in `decided.json`, runs a
   thorough secret scan, and prints a compact JSON digest. The injection
   scanner is RSI's `scan_content.py`, looked up at a short list of known
   paths (direct RSI install first, plugin-marketplace install second);
   `scan_status` in the digest says exactly which passes ran.

3. **Evaluate** — a cron line at **17:30 daily** runs `run-evaluate.sh`, which
   runs `aggregate.py` itself, publishes `digest-latest.json` atomically
   (keeping the previous run as `digest-prev.json`), sweeps ledger files older
   than 6 months, and only then invokes `claude --print` over `prompt.md`. The
   agent is **read-only** — `Read Glob Grep`, no `Bash`, no `Write`. It
   classifies each group as `bad-config`, `sensitive`, `wrong-tool` or
   `rule-bug` and emits at most 5 proposals as delimited stdout blocks.
   `proposal-sink.py` parses those blocks and writes the files into
   `~/.claude/proposals/permissions/`. Log:
   `~/.claude/logs/permission-ledger.log`.

   Why the aggregation moved out of the agent: `permissions.deny` contains a
   blanket `"Bash"`, and deny beats allow, so the old
   `--allowedTools "… Bash(python3 …/aggregate.py*)"` grant could never have
   worked — the nightly run would have hit its failure branch every night. If
   the aggregate fails or the digest holds zero groups, `run-evaluate.sh` logs
   one line and exits without paying for a model call.

   Why the proposal writing moved out too (**2026-08-03**): the same reasoning
   one step further. The original design granted
   `--allowedTools "… Write(~/.claude/proposals/permissions/*)"`, which is dead
   by construction headless — path-scoped `Write(...)` grants never register
   under `--print`, and every path under `~/.claude/` is refused by Claude
   Code's built-in sensitive-path gate regardless. It went unnoticed for two
   days because the digest held zero groups; the first night it held five, the
   agent wrote nothing and said so in the log. The sink is the same shape
   `rsiProposalSink` uses in `home/jonathan-linux.nix`, which is the one path
   proven to work here: proposer emits, pipeline persists. The sink enforces
   the `YYYY-MM-DD-<slug>.md` filename shape (no traversal), never overwrites
   an existing proposal, and caps bodies at 64 KiB and runs at 5 proposals.

## Switching it off

The evaluator reads `components.permission_ledger` from
`~/.claude/recursive-self-improvement/config/config.json` before it does
anything else — no digest, no model call, one log line, exit 0 when it is
`false`. Toggling is a JSON edit; capture (stage 1) keeps running regardless,
so the ledger stays complete while the nightly evaluation is paused.

A missing file, missing key or unparseable JSON reads as **on**: capture is
already running and the aggregate is free, so a config typo must not silently
stop the only stage that surfaces any of this to a human. RSI's `daily_review`
switch defaults the other way (absent = off) because that one spends an Opus
pass per night. Cheap-and-visible fails on, expensive fails off.

## Where things live

```
~/.claude/permission-ledger/
├── hook.py            stage 1
├── aggregate.py       stage 2
├── run-evaluate.sh    the cron entrypoint: stage 2 + stage 3 + retention
├── prompt.md          stage 3 — the evaluator prompt
├── proposal-sink.py   stage 3 — parses the agent's stdout blocks into files
├── README.md          this file
├── YYYY-MM.jsonl      the ledger, one month per file, 0600
├── digest-latest.json what the nightly agent reads, 0600
├── digest-prev.json   the run before that, kept for diffing
├── decided.json       pattern_key -> your decision, written by record-decision.py
└── hook-errors.log    anything the hook swallowed
~/.claude/proposals/permissions/
├── YYYY-MM-DD-<slug>.md     pending proposals
└── archived/                drained proposals
~/.claude/_backups/settings.json.<timestamp>.bak
```

The ledger directory is `0700`; ledger files are `0600`.

## Inspecting it by hand

```sh
# what fired today
grep "$(date +%F)" ~/.claude/permission-ledger/$(date +%Y-%m).jsonl | jq -c '{ts,tool_name,pattern_key,event}'

# top friction, last month, no model involved
jq -r .pattern_key ~/.claude/permission-ledger/$(date +%Y-%m).jsonl | sort | uniq -c | sort -rn | head -20

# what you actually declined
jq -c 'select(.event=="denied") | {ts,pattern_key,reason}' ~/.claude/permission-ledger/*.jsonl

# what the nightly run would see, right now
python3 ~/.claude/permission-ledger/aggregate.py | jq .

# what last night's run actually handed the agent
jq . ~/.claude/permission-ledger/digest-latest.json

# what the nightly job did, and whether it even called the model
grep permission-ledger ~/.claude/logs/permission-ledger.log | tail -20

# did the hook ever fail
cat ~/.claude/permission-ledger/hook-errors.log
```

Run the whole nightly job by hand — aggregation, digest, model call and all:

```sh
~/.claude/permission-ledger/run-evaluate.sh; tail -20 ~/.claude/logs/permission-ledger.log
```

## Draining proposals

New proposals appear in the SessionStart nudge automatically —
`~/.claude/proposals/` auto-discovers subdirs, so `permissions/` needs no plugin
or config edit. Non-`rsi` subdirs are counted by file existence.

Run **`/review-improvements`** and walk them. For each one, read the
**Blast radius** section before the proposed change — that section is the whole
basis for the decision, and a vague one is grounds to reject the proposal
outright rather than the rule.

Drain by moving the file:

```sh
mv ~/.claude/proposals/permissions/2026-08-01-read-home-jonathan-claude.md \
   ~/.claude/proposals/permissions/archived/
```

Then record the outcome, or the pattern comes back in tomorrow's digest and
every digest after it:

```sh
~/.claude/permission-ledger/record-decision.py \
  'Read:/home/jonathan/.claude' implemented --count 47
```

`--count` is the number from the proposal's `## Friction` line. It is what makes
the single re-ask work: a `rejected` pattern returns exactly once, if its count
later triples. Omit `--count` and the pattern is suppressed permanently.

**Wired (2026-08-01).** `/review-improvements` now runs
`~/.claude/proposals/permissions/on-decision <proposal> <outcome>` after each
recorded decision (implemented/accepted → `implemented`; rejected and skipped →
`rejected`; deferred → nothing recorded, pattern stays live; `--count`
extracted from the `## Friction` line when present). The mapping lives in
`~/.claude/proposals/gate-config.json` under `on_decision.permissions` — the
skill reads that map, so re-pointing or disabling the callback is a config
edit, not a skill edit. Draining by hand still requires the manual
`record-decision.py` call above.

## Retention

The ledger rotates monthly by filename, and `run-evaluate.sh` sweeps it at the
top of every nightly run: any `YYYY-MM.jsonl` whose month is more than 6 months
behind the current one is deleted, and each deletion is logged. The sweep runs
before the aggregate, so it still happens on nights when the aggregate or the
model call fails. Nothing else is ever deleted — `decided.json`, the digests
and the error log stay.

To do it by hand anyway:

```sh
find ~/.claude/permission-ledger -name '*.jsonl' -mtime +180 -delete
```

Deleting the whole ledger at any time is safe — it only costs you history.

## Rollback

```sh
bash /path/to/permission-ledger/uninstall.sh
```

Removes both hook entries from `settings.json` (leaving every other hook
intact), removes the cron line by its `# permission-ledger-evaluate` tag, and
leaves the ledger and your proposals alone. `--purge` also deletes
`~/.claude/permission-ledger/`; proposals are never deleted.

`settings.json` is snapshotted to `~/.claude/_backups/settings.json.<ts>.bak`
before either script touches it, so the blunt rollback is:

```sh
cp ~/.claude/_backups/settings.json.<timestamp>.bak ~/.claude/settings.json
crontab -l | grep -v '# permission-ledger-evaluate' | crontab -
```
