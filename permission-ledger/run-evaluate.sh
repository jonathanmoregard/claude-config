#!/usr/bin/env bash
# Permission Friction Ledger — nightly evaluate entrypoint (stage 2 + stage 3).
#
# This is what cron runs. It does the deterministic work itself, in plain shell,
# and only then hands a finished digest to the model:
#
#   1. component gate — is the ledger switched on in the RSI config at all
#   2. retention sweep — drop YYYY-MM.jsonl ledger files older than 6 months
#   3. run aggregate.py, atomically publishing digest-latest.json
#      (previous digest kept as digest-prev.json)
#   4. bail out, without paying for a model call, when there is nothing to say
#   5. otherwise run `claude --print` over prompt.md, READ-ONLY, and hand its
#      stdout to proposal-sink.py, which writes the proposal files
#
# Why the aggregation is here and not inside the agent: the agent must not have a
# shell, and keeping it shell-less is the job of `--tools` (AGENT_TOOLS) plus
# DISALLOWED_TOOLS below, NOT of the positive `--allowedTools` list.
#
# THIRD MEASUREMENT, 2026-09-03, CLI 2.1.259 — a deny list is not a closure.
# The host CLI auto-updated 2.1.251 -> 2.1.259, and `--disallowedTools` removes
# only the tools it NAMES, so this containment was exactly as complete as the
# roster on the day it was written. A live probe of this invocation shape on
# 2.1.259 returned the offer
#   ["Glob","Grep","ListAgents","LSP","Read","ReportFindings","Skill","ToolSearch"]
# Subtract the deny list and Read/Glob/Grep and `Skill` survives — and a skill
# may run in a SUBAGENT, which gets its own tool set and so transitively reaches
# Bash, Write and the network. That is precisely the delegation `Task`/`Agent`
# were denied for, arriving under a name nobody had written down. `ToolSearch`
# survived too, and its purpose is to make deferred tools callable.
#
# Hence `--tools`: it CLOSES the offer to the names it lists, so a tool the NEXT
# CLI adds is not offered at all. Both rails ship, because `--tools` silently
# ignores an unknown name and would fail open on a typo.
#
# The fix, measured the same way on the same CLI — the offer for the invocation
# BELOW is exactly
#   ["Glob","Grep","Read"]
# with mcp_servers []. Nothing remains after subtracting what the agent needs,
# so `Skill` and `ToolSearch` are gone by closure and not merely by name.
#
# That distinction cost a real hole and is worth stating flatly:
# `--allowedTools` PRE-APPROVES the tools it names. It does not REMOVE the ones
# it omits. The original argument here was a blanket `"Bash"` in
# `permissions.deny` beating any allow — which was true, and was the only thing
# actually holding the line. When safe-bash was retired, `Bash` moved into
# `permissions.allow`, that deny went away, and the allowlist was left holding a
# line it never held.
#
# MEASURED against CLI 2.1.251 with this branch's settings.json:
# `--allowedTools 'Read Glob Grep'` still offered Bash, Write, Edit,
# NotebookEdit, Task, Cron* and the MCP tools, and the agent ran `id -u` -> 1000
# unsandboxed and wrote a file outside its working directory. This agent's input
# is untrusted session material, so that is the worst possible place for a shell.
#
# Two dead ends, both measured, so nobody re-walks them:
#   * `--setting-sources ''` does NOT fix it — Bash is still offered and still
#     executes, and dropping the settings file drops `permissions.deny` with it,
#     so WebFetch/WebSearch come BACK.
#   * `--disallowedTools` is the flag that actually removes a tool, but it
#     removes only what it names. Hence an explicit list, pinned by
#     tests/test_bash_guards.py so a new tool cannot quietly join the offer.
#
# Why the proposal writing is here and not inside the agent either: the same
# reasoning one step further. This script used to pass
# `Write(~/.claude/proposals/permissions/*)`, which is dead by construction
# headless — path-scoped Write grants never register under `--print`, and
# ~/.claude sits behind Claude Code's sensitive-path gate regardless. The first
# night the digest held groups (2026-08-03, 5 groups) the agent wrote zero
# files. The agent now emits delimited blocks on stdout and proposal-sink.py
# persists them: proposer emits, pipeline persists.
#
# Usage:
#   run-evaluate.sh            # what cron does
#
# Environment:
#   PERMISSION_LEDGER_DIR  ledger directory (same variable hook.py honours).
#                          Default ~/.claude/permission-ledger.
#   PL_CLAUDE_CMD          the claude binary to invoke. Default `claude`.
#                          Exists so the end-to-end test can point it at a shim.
#
# Exit codes:
#   0  normal run (including "nothing to evaluate" and "switched off")
#   1  aggregate.py failed or emitted unparseable JSON; nothing was published
#   2  the agent was invoked and exited non-zero
#   3  the agent succeeded but the sink could not run; raw output preserved
#
# Everything this script has to say goes to ~/.claude/logs/permission-ledger.log
# with a UTC timestamp prefix, one line at a time, so the log stays greppable.

set -uo pipefail   # deliberately not -e: every failure branch is handled here

CLAUDE_HOME="$HOME/.claude"

LEDGER_DIR="${PERMISSION_LEDGER_DIR:-$CLAUDE_HOME/permission-ledger}"
LEDGER_DIR="${LEDGER_DIR/#\~/$HOME}"

LOG_DIR="$CLAUDE_HOME/logs"
LOG_FILE="$LOG_DIR/permission-ledger.log"

# Raw agent output on a failed run is forensics, not a log line, and it does
# NOT go in $LOG_DIR. That directory lives inside the live config git repo,
# whose .gitignore covers `logs/*.log` and nothing else — so a `.txt` sibling
# left an UNTRACKED file in ~/.claude on every failure night. Beyond the
# dirtying: this content is raw model output derived from untrusted transcript
# samples and has no business inside the config the harness reads. State dir.
RAW_KEEP_DIR="$HOME/.local/state/permission-ledger"

AGGREGATE="$LEDGER_DIR/aggregate.py"
PROMPT_FILE="$LEDGER_DIR/prompt.md"
SINK="$LEDGER_DIR/proposal-sink.py"
DIGEST="$LEDGER_DIR/digest-latest.json"
DIGEST_PREV="$LEDGER_DIR/digest-prev.json"
RSI_CONFIG="$CLAUDE_HOME/recursive-self-improvement/config/config.json"

# Where proposals live. They moved out of ~/.claude on 2026-08-31 (the nightly
# runs dirtied the live config checkout the harness reads); the sink is a git
# repo of its own. ~/.claude/proposals survives as a symlink to it, but this is
# a WRITER, so it resolves the real path — a writer that depends on a symlink
# existing is a writer that fails silently the day the symlink does not.
# Source of truth: recursive-self-improvement/config/config.json ->
# proposals.folder, with the canonical literal as the fallback.
PROPOSALS_SINK_DEFAULT="$HOME/.local/state/claude-proposals"
resolve_proposals_sink() {
  python3 -S -E - "$RSI_CONFIG" "$PROPOSALS_SINK_DEFAULT" <<'PY' 2>/dev/null \
    || printf '%s\n' "$PROPOSALS_SINK_DEFAULT"
import json
import os
import sys

fallback = sys.argv[2]
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        value = json.load(fh).get("proposals", {}).get("folder") or fallback
except Exception:                                             # noqa: BLE001
    value = fallback
value = os.path.expanduser(str(value))
print(value if os.path.isabs(value) else fallback)
PY
}
PROPOSALS_SINK="$(resolve_proposals_sink)"
PROPOSALS_DIR="$PROPOSALS_SINK/permissions"

CLAUDE_CMD="${PL_CLAUDE_CMD:-claude}"

# What the agent legitimately needs. This is the POSITIVE half and it is not a
# security boundary on its own — see the header. Running aggregate.py out here
# is what lets the list stay this short; proposal-sink.py persisting the output
# is what keeps Write off it.
ALLOWED_TOOLS='Read Glob Grep'

# The CLOSURE, and the rail that actually bounds this agent. `--tools` limits
# the offer to the built-in names listed here, so a tool a FUTURE CLI adds is
# never offered at all — which is the difference between this and the deny list
# below. Measured 2026-09-03 against 2.1.259: with the deny list alone the live
# offer still carried `Skill` and `ToolSearch` (see the header).
#
# Not sufficient on its own: an unknown tool NAME in `--tools` is SILENTLY
# IGNORED (verified against 2.1.222, recorded in scripts/idle-handoff.sh), so a
# typo here fails open and the deny list has to repeat the closure by name.
AGENT_TOOLS='Read Glob Grep'

# The half that removes NAMED tools, and the second copy of the closure above.
# Everything that mutates the filesystem, delegates to another agent, schedules
# future work, or reaches the network — for an agent whose entire input is
# untrusted permission-denial records.
#
# `Skill` and `ToolSearch` are here because the 2.1.259 probe found them in the
# live offer: a skill may run in a SUBAGENT (the roster carries schedule, loop,
# run-at, orchestrate), and a subagent gets its own tool set, so `Skill` is the
# same delegation `Task` and `Agent` are denied for. `ToolSearch` exists to make
# deferred tools callable, which widens whatever this list misses. `SlashCommand`
# is NOT in the 2.1.259 offer and is named defensively.
#
# Read/Glob/Grep are deliberately absent so the two lists cannot contradict.
# tests/test_bash_guards.py pins the load-bearing names and asserts the two
# lists stay disjoint.
DISALLOWED_TOOLS='Bash Write Edit MultiEdit NotebookEdit Task Agent Skill SlashCommand ToolSearch Workflow SendMessage RemoteTrigger CronCreate CronDelete CronList ScheduleWakeup EnterWorktree ExitWorktree DesignSync Artifact TaskStop TaskOutput WebFetch WebSearch'

RETENTION_MONTHS=6

mkdir -p "$LOG_DIR" 2>/dev/null || true

log() {
  printf '%s permission-ledger: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> "$LOG_FILE"
}

# -------------------------------------------------------- component gate ----
# RSI's parts are individually switchable from one file
# (recursive-self-improvement/config/config.json → components.*), so turning
# this evaluator off is a JSON edit, not a crontab PR.
#
# A missing file, a missing key, or unparseable JSON all read as ENABLED here,
# deliberately: capture is already running, the aggregate is free, and this is
# the only stage that turns the ledger into something a human ever sees — a
# config typo must not silently stop it. The daily-review wrapper defaults the
# other way (absent config = off) because that one spends an Opus pass per
# night. Cheap-and-visible fails on, expensive fails off.
component_enabled() {
  python3 -S -E - "$RSI_CONFIG" <<'PY' 2>/dev/null || echo true
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        value = json.load(fh).get("components", {}).get("permission_ledger", True)
except Exception:                                             # noqa: BLE001
    value = True
off = value in (False, 0, None) or (
    isinstance(value, str) and value.strip().lower() in ("false", "0", "no", "off")
)
print("false" if off else "true")
PY
}

if [[ "$(component_enabled)" != "true" ]]; then
  log "components.permission_ledger is off in $RSI_CONFIG; skipping this tick"
  exit 0
fi

# ------------------------------------------------------------------ lock ----
# One run at a time. Without this, a run that hangs on the model call is still
# alive at the next 17:30 tick and the two interleave; the digest publish is
# rename-atomic so the damage is bounded, but two agents writing proposals into
# the same directory is not something to leave to chance. Non-blocking: a
# skipped tick is the right outcome, since the next one is 24h away and the
# ledger is cumulative.

mkdir -p "$LEDGER_DIR" 2>/dev/null || true
if command -v flock >/dev/null 2>&1; then
  if exec 9>"$LEDGER_DIR/.evaluate.lock" 2>/dev/null; then
    if ! flock -n 9; then
      log "another run holds the lock; skipping this tick"
      exit 0
    fi
  else
    log "warning: could not open the lock file; proceeding unlocked"
  fi
fi

# ------------------------------------------------------------- retention ----
# Runs first, and unconditionally: it must still happen on days when the
# aggregate or the agent call fails, otherwise a broken evaluator would also
# silently stop the ledger from ever being pruned.

sweep_retention() {
  local now_idx keep_from f base y m idx
  now_idx=$(( 10#$(date -u +%Y) * 12 + 10#$(date -u +%m) - 1 ))
  keep_from=$(( now_idx - RETENTION_MONTHS ))
  shopt -s nullglob
  for f in "$LEDGER_DIR"/*.jsonl; do
    base="$(basename "$f")"
    [[ "$base" =~ ^([0-9]{4})-([0-9]{2})\.jsonl$ ]] || continue
    y="${BASH_REMATCH[1]}"
    m="${BASH_REMATCH[2]}"
    idx=$(( 10#$y * 12 + 10#$m - 1 ))
    if (( idx < keep_from )); then
      if rm -f -- "$f"; then
        log "retention: deleted $base (older than $RETENTION_MONTHS months)"
      else
        log "retention: FAILED to delete $base"
      fi
    fi
  done
  shopt -u nullglob
}

sweep_retention

# ------------------------------------------------------------- aggregate ----

if [[ ! -f "$AGGREGATE" ]]; then
  log "aggregate.py not found at $AGGREGATE; no digest, agent not invoked"
  exit 1
fi

tmp_new="$LEDGER_DIR/.digest-latest.json.$$"
tmp_prev="$LEDGER_DIR/.digest-prev.json.$$"
tmp_err="$LEDGER_DIR/.digest-latest.err.$$"
raw="$LEDGER_DIR/.agent-raw.$$"      # written only if the agent actually runs
cleanup() { rm -f -- "$tmp_new" "$tmp_prev" "$tmp_err" "$raw"; }
trap cleanup EXIT

# Digests contain sampled tool inputs, so they are written 0600 like the ledger
# itself. Restored before the agent runs so its proposals keep normal modes.
old_umask="$(umask)"
umask 077

python3 "$AGGREGATE" --ledger-dir "$LEDGER_DIR" \
  --pending-dir "$PROPOSALS_SINK/permissions" > "$tmp_new" 2>"$tmp_err"
status=$?
if [[ $status -ne 0 ]]; then
  while IFS= read -r line; do
    [[ -n "$line" ]] && log "aggregate stderr: $line"
  done < "$tmp_err"
  log "aggregate.py exited $status; digest not updated, agent not invoked"
  exit 1
fi

# Validate before publishing: a half-written or non-JSON file must never
# replace yesterday's good digest, and must never reach the model.
stats="$(python3 -S -E - "$tmp_new" 2>&1 <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        digest = json.load(fh)
except Exception as exc:                                    # noqa: BLE001
    print("%s: %s" % (type(exc).__name__, exc), file=sys.stderr)
    raise SystemExit(1)
if not isinstance(digest, dict) or not isinstance(digest.get("groups"), list):
    print("digest is not an object with a 'groups' list", file=sys.stderr)
    raise SystemExit(1)
print("%d %s %s" % (
    len(digest["groups"]),
    digest.get("total_records", "?"),
    digest.get("malformed_lines", "?"),
))
PY
)"
status=$?
if [[ $status -ne 0 ]]; then
  log "aggregate.py produced unparseable JSON (${stats//$'\n'/ }); digest not updated, agent not invoked"
  exit 1
fi

read -r n_groups n_records n_malformed <<< "$stats"

# Publish atomically. Both moves are renames inside the ledger directory, so a
# reader either sees the old file or the new one, never a partial one.
if [[ -f "$DIGEST" ]]; then
  if cp -p -- "$DIGEST" "$tmp_prev" && mv -f -- "$tmp_prev" "$DIGEST_PREV"; then
    :
  else
    log "warning: could not rotate the previous digest to $DIGEST_PREV"
  fi
fi
if ! mv -f -- "$tmp_new" "$DIGEST"; then
  log "failed to publish $DIGEST; agent not invoked"
  exit 1
fi
chmod 0600 "$DIGEST" 2>/dev/null || true
umask "$old_umask"
log "digest written: $n_groups group(s), $n_records record(s) in window, $n_malformed malformed line(s)"

# ------------------------------------------------------------ early exits ----

if [[ "$n_groups" -eq 0 ]]; then
  log "no groups above threshold; agent not invoked"
  exit 0
fi

if [[ ! -f "$PROMPT_FILE" ]]; then
  log "prompt.md not found at $PROMPT_FILE; agent not invoked"
  exit 1
fi

# ---------------------------------------------------------------- evaluate ---

# Bounded so a wedged model call cannot hold the lock until someone notices.
# 30 minutes is far beyond any honest run over a <=40-group digest; a run that
# reaches it is stuck, not slow.
TIMEOUT_SECONDS=1800
if command -v timeout >/dev/null 2>&1; then
  run_agent=(timeout "$TIMEOUT_SECONDS" "$CLAUDE_CMD")
else
  run_agent=("$CLAUDE_CMD")
fi

log "invoking $CLAUDE_CMD over $n_groups group(s)"
# Raw model output is shaped by untrusted session material (the digest samples
# real tool inputs), so it lands 0600 in the ledger dir and is deleted on exit
# unless the sink reports an anomaly worth keeping.
old_umask="$(umask)"
umask 077
# CLAUDE_PIPELINE_INTERNAL: this is a machine session, so session-reflect must
# not mint a reflection of it into the corpus the RSI review reads.
( cd "$CLAUDE_HOME" 2>/dev/null && CLAUDE_PIPELINE_INTERNAL=1 "${run_agent[@]}" \
  --model opus \
  --print \
  --tools "$AGENT_TOOLS" \
  --allowedTools "$ALLOWED_TOOLS" \
  --disallowedTools "$DISALLOWED_TOOLS" \
  --strict-mcp-config \
  -p "$(cat "$PROMPT_FILE")" ) > "$raw" 2>&1
status=$?
umask "$old_umask"

if [[ $status -eq 124 ]]; then
  log "agent exceeded ${TIMEOUT_SECONDS}s and was killed; proposals may be partial"
fi

if [[ $status -ne 0 ]]; then
  # Log what it managed to say before failing — the failure reason is usually
  # in there — but do not persist proposals out of a failed run.
  while IFS= read -r line; do
    [[ -n "$line" ]] && log "agent: $line"
  done < "$raw"
  log "agent exited $status"
  exit 2
fi

# ------------------------------------------------------------------ sink ----

if [[ ! -x "$SINK" && ! -f "$SINK" ]]; then
  keep="$RAW_KEEP_DIR/permission-ledger-raw-$(date -u +%Y%m%dT%H%M%SZ).txt"
  mkdir -p "$RAW_KEEP_DIR" 2>/dev/null \
    && cp -- "$raw" "$keep" 2>/dev/null \
    && log "raw agent output preserved at $keep"
  log "proposal-sink.py not found at $SINK; nothing persisted"
  exit 3
fi

sink_out="$(python3 -S -E "$SINK" "$PROPOSALS_DIR" < "$raw" 2>&1)"
sink_rc=$?
while IFS= read -r line; do
  [[ -n "$line" ]] && log "$line"
done <<< "$sink_out"

if [[ $sink_rc -ne 0 ]]; then
  keep="$RAW_KEEP_DIR/permission-ledger-raw-$(date -u +%Y%m%dT%H%M%SZ).txt"
  if mkdir -p "$RAW_KEEP_DIR" 2>/dev/null && cp -- "$raw" "$keep" 2>/dev/null; then
    log "raw agent output preserved at $keep"
  else
    log "warning: could not preserve raw agent output to $keep"
  fi
  if [[ $sink_rc -ne 2 ]]; then
    # rc 2 is "wrote what it could, some block was rejected" — a bad model
    # night, not a broken pipeline. Anything else means the sink itself died.
    log "sink failed rc=$sink_rc"
    exit 3
  fi
fi

log "agent exited 0"
exit 0
