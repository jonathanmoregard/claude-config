#!/usr/bin/env bash
# Permission Friction Ledger — nightly evaluate entrypoint (stage 2 + stage 3).
#
# This is what cron runs. It does the deterministic work itself, in plain shell,
# and only then hands a finished digest to the model:
#
#   1. retention sweep — drop YYYY-MM.jsonl ledger files older than 6 months
#   2. run aggregate.py, atomically publishing digest-latest.json
#      (previous digest kept as digest-prev.json)
#   3. bail out, without paying for a model call, when there is nothing to say
#   4. otherwise run `claude --print` over prompt.md
#
# Why the aggregation is here and not inside the agent: `permissions.deny` in
# ~/.claude/settings.json contains a blanket "Bash", and deny beats allow, so a
# `--allowedTools "Bash(python3 .../aggregate.py*)"` grant can never take
# effect. The agent therefore gets no Bash at all — it Reads the digest file.
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
#   0  normal run (including "nothing to evaluate")
#   1  aggregate.py failed or emitted unparseable JSON; nothing was published
#   2  the agent was invoked and exited non-zero
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
DIGEST="$LEDGER_DIR/digest-latest.json"
DIGEST_PREV="$LEDGER_DIR/digest-prev.json"

CLAUDE_CMD="${PL_CLAUDE_CMD:-claude}"

# No Bash(...) entry. That is the entire point of running aggregate.py out here.
ALLOWED_TOOLS='Read Glob Grep Write(~/.claude/proposals/permissions/*)'

RETENTION_MONTHS=6

mkdir -p "$LOG_DIR" 2>/dev/null || true

log() {
  printf '%s permission-ledger: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> "$LOG_FILE"
}

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
cleanup() { rm -f -- "$tmp_new" "$tmp_prev" "$tmp_err"; }
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
out="$(cd "$CLAUDE_HOME" 2>/dev/null && "${run_agent[@]}" \
  --model opus \
  --print \
  --allowedTools "$ALLOWED_TOOLS" \
  -p "$(cat "$PROMPT_FILE")" 2>&1)"
status=$?

if [[ $status -eq 124 ]]; then
  log "agent exceeded ${TIMEOUT_SECONDS}s and was killed; proposals may be partial"
fi

while IFS= read -r line; do
  [[ -n "$line" ]] && log "agent: $line"
done <<< "$out"

if [[ $status -ne 0 ]]; then
  log "agent exited $status"
  exit 2
fi
log "agent exited 0"
exit 0
