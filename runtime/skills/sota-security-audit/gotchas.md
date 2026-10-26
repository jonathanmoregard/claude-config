# Gotchas — tools and harness (read before phase 3)

## Command guards (dcg / deny globs)
- Redirect targets built from `$VAR` / `$(…)` are blocked (`redirect-truncate-dynamic-path`), and so is `git -C $var` inside loops. Write files with the Write tool, and put loops in a script file that uses literal paths.
- Deny globs match the whole command STRING. A script *name* can trip them (e.g. `*prompt*.jsonl*`), so rename the helper rather than quoting around the match.
- Before deploying a guard/deny change, replay ~3k real historical commands through it under the old and new config, and report how many verdicts changed.

## Long jobs
- Run tool batches with `systemd-run --user --unit=<name> -p MemoryMax=… -p RuntimeMaxSec=…`. A gitleaks/trufflehog `dir` scan over GB-sized agent transcripts gets OOM-killed, so scope it (last N days, chunked).
- Check `systemctl --user is-active <unit>` before relaunching. Two copies of a batch writing the same outputs will clobber each other.
- `wait-for unit` needs `--user` and can return at once for a transient unit that is still running. To wait for the unit to exit, use a background `while systemctl --user is-active -q …; do sleep 20; done`.

## Scanners
- trufflehog verifies candidates by calling third-party APIs. Use `--no-verification` unless you mean it; "verified" hits on `test_*` names are false positives.
- gitleaks/betterleaks flag `.secrets.baseline` hashes and doc placeholders. Triage them; don't count them.
- vulnix matches on package names and produces many false positives. Use `vulnxscan` (vulnix+grype+OSV) and keep rows where `sum ≥ 2`.
- mcp-scanner `--scan-known-configs` may find 0 tools for Claude Code's config layout. Pass the servers explicitly, or record `skipped:<reason>`.
- Snyk agent-scan uploads configs and skills to Snyk. Get user consent first.
- A clean scan is not a negative for what the scanner doesn't model.
- "Method offered" is not "method accepts". An sshd that advertises keyboard-interactive can be backed by `pam_deny`, so check the backing stack.

## Cross-model jobs (ai-router)
- `ai-router result` truncates `result_text` at 4000 chars, and `result_json` can be null even with `--schema`. Read `~/.local/state/ai-router/jobs/<id>/last_message.md` and `raw_decode` from the first `{`.
- Codex "read-only" may still run a writable managed profile if `/etc/codex/requirements.toml` pins one. Check the job header's `sandbox:` line.
- A PreToolUse gate can rewrite the Agent `model` to an invalid alias. If the call fails schema validation, omit `model`.
- Codex sandbox lanes often commit code that was never compiled or tested. Rebuild and retest every lane yourself, and diff its reported items against the brief.
- Route by quota pacing (`ai-router pick`). Push bulk reviewing and refuting to whichever provider is ahead of pace.

## Oracles that worked
- Synthetic hook events piped into a guard script, reporting allow/deny per command form, including wrapper forms (`bash -c`, `eval`, `$(…)`).
- Membership queries against a public binary cache for private store paths.
- History replay: count real calls a new control would have changed (transcripts, shell history, logs).
- Headless `claude -p` with old vs new flags plus an injection canary ("run `touch <scratch>/canary`") to show which tools are actually offered. Read the stream-json init event for the real tool list. `--allowedTools` only pre-approves tools; `--tools` + `--strict-mcp-config` remove them.
- Hash-compare leaked-token candidates against live tokens in-process and print only fingerprints.
