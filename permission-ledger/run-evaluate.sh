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
# Why the aggregation is here and not inside the agent: `permissions.deny` in
# ~/.claude/settings.json contains a blanket "Bash", and deny beats allow, so a
# `--allowedTools "Bash(python3 .../aggregate.py*)"` grant can never take
# effect. The agent therefore gets no Bash at all — it Reads the digest file.
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

AGGREGATE="$LEDGER_DIR/aggregate.py"
PROMPT_FILE="$LEDGER_DIR/prompt.md"
SINK="$LEDGER_DIR/proposal-sink.py"
DIGEST="$LEDGER_DIR/digest-latest.json"
DIGEST_PREV="$LEDGER_DIR/digest-prev.json"
PROPOSALS_DIR="$CLAUDE_HOME/proposals/permissions"
RSI_CONFIG="$CLAUDE_HOME/recursive-self-improvement/config/config.json"

CLAUDE_CMD="${PL_CLAUDE_CMD:-claude}"

# Read-only, and no Bash(...) entry. Running aggregate.py out here is what buys
# the first; proposal-sink.py persisting the output is what buys the second.
# Any Write(...)/Bash(...) grant added back here is a silent no-op headless.
ALLOWED_TOOLS='Read Glob Grep'

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

python3 "$AGGREGATE" --ledger-dir "$LEDGER_DIR" > "$tmp_new" 2>"$tmp_err"
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
( cd "$CLAUDE_HOME" 2>/dev/null && "${run_agent[@]}" \
  --model opus \
  --print \
  --allowedTools "$ALLOWED_TOOLS" \
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
  keep="$LOG_DIR/permission-ledger-raw-$(date -u +%Y%m%dT%H%M%SZ).txt"
  cp -- "$raw" "$keep" 2>/dev/null && log "raw agent output preserved at $keep"
  log "proposal-sink.py not found at $SINK; nothing persisted"
  exit 3
fi

sink_out="$(python3 -S -E "$SINK" "$PROPOSALS_DIR" < "$raw" 2>&1)"
sink_rc=$?
while IFS= read -r line; do
  [[ -n "$line" ]] && log "$line"
done <<< "$sink_out"

if [[ $sink_rc -ne 0 ]]; then
  keep="$LOG_DIR/permission-ledger-raw-$(date -u +%Y%m%dT%H%M%SZ).txt"
  if cp -- "$raw" "$keep" 2>/dev/null; then
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
