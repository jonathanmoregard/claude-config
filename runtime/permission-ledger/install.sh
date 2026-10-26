#!/usr/bin/env bash
# Permission Friction Ledger — installer.
#
# Installs the three stages described in README.md:
#   1. capture   — hook.py, registered on PermissionRequest + PermissionDenied
#   2. aggregate — aggregate.py, run by run-evaluate.sh (not by the agent)
#   3. evaluate  — a nightly cron line running run-evaluate.sh, which publishes
#                  the digest and then runs claude --print over prompt.md
#
# Idempotent: safe to run any number of times. Snapshots settings.json before
# touching it. Never removes a pre-existing hook, permission or top-level key.
#
# Usage:
#   bash install.sh [--no-cron]
#
# Environment:
#   PL_CRONTAB  override the crontab binary (used by the test harness to
#               redirect the crontab step at a shim). When unset and $HOME does
#               not look like the invoking user's real home, the crontab step is
#               skipped entirely rather than guessed at.

set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

TARGET="$HOME/.claude"
LEDGER_DIR="$TARGET/permission-ledger"
PROPOSALS_DIR="$TARGET/proposals/permissions"
ARCHIVE_DIR="$PROPOSALS_DIR/archived"
BACKUP_DIR="$TARGET/_backups"
LOG_DIR="$TARGET/logs"
SETTINGS="$TARGET/settings.json"

# Matched as a SUBSTRING of the registered command, never as the whole string,
# so changing the interpreter flags below does not orphan an existing entry.
HOOK_MARKER="permission-ledger/hook.py"
# -S skips site processing and -E ignores PYTHONPATH: ~80 ms of the ~150 ms
# per-dialog cost is bare interpreter startup, and hook.py is stdlib-only, so
# both flags are safe and free.
HOOK_COMMAND='python3 -S -E $HOME/.claude/permission-ledger/hook.py'
HOOK_TIMEOUT=5

CRON_TAG="# permission-ledger-evaluate"
# The cron entrypoint is run-evaluate.sh, which runs aggregate.py itself and
# then invokes the agent with no Bash allowance at all. Handing the aggregation
# to the agent cannot work: permissions.deny carries a blanket "Bash", deny
# beats allow, so a Bash(python3 .../aggregate.py*) grant is dead on arrival.
# Single-quoted so nothing is expanded at install time; cron expands $HOME.
CRON_LINE='30 17 * * * $HOME/.claude/permission-ledger/run-evaluate.sh >> $HOME/.claude/logs/permission-ledger.log 2>&1 # permission-ledger-evaluate'

WANT_CRON=true
for arg in "$@"; do
  case "$arg" in
    --no-cron) WANT_CRON=false ;;
    -h|--help) sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "install.sh: unknown argument: $arg" >&2; exit 2 ;;
  esac
done

CHANGES=()
note() { CHANGES+=("$1"); }

# ---------------------------------------------------------------- preflight --

for f in hook.py aggregate.py run-evaluate.sh proposal-sink.py record-decision.py prompt.md README.md; do
  if [[ ! -f "$SRC_DIR/$f" ]]; then
    echo "install.sh: missing required file next to the installer: $SRC_DIR/$f" >&2
    exit 1
  fi
done

if ! command -v python3 >/dev/null 2>&1; then
  echo "install.sh: python3 not found on PATH; required for the settings.json edit" >&2
  exit 1
fi

# --------------------------------------------------- crontab safety decision --

# Skip the crontab step when $HOME has been redirected (test harness, container,
# someone poking at a copy), unless PL_CRONTAB explicitly says what to use.
REAL_HOME="$(getent passwd "$(id -un)" 2>/dev/null | cut -d: -f6 || true)"
CRONTAB_CMD=""
CRON_SKIP_REASON=""

if [[ "$WANT_CRON" != true ]]; then
  CRON_SKIP_REASON="--no-cron passed"
elif [[ -n "${PL_CRONTAB:-}" ]]; then
  CRONTAB_CMD="$PL_CRONTAB"
elif [[ -n "$REAL_HOME" && "$HOME" != "$REAL_HOME" ]]; then
  CRON_SKIP_REASON="\$HOME ($HOME) is not this user's real home ($REAL_HOME)"
elif ! command -v crontab >/dev/null 2>&1; then
  CRON_SKIP_REASON="crontab not found on PATH"
else
  CRONTAB_CMD="crontab"
fi

# ------------------------------------------------------------- directories ---

mkdir -p "$LEDGER_DIR"
chmod 0700 "$LEDGER_DIR"
mkdir -p "$PROPOSALS_DIR" "$ARCHIVE_DIR" "$BACKUP_DIR" "$LOG_DIR"
note "ensured $LEDGER_DIR (0700), $PROPOSALS_DIR, $ARCHIVE_DIR, $LOG_DIR"

# ------------------------------------------------------------------ payload --

for f in hook.py aggregate.py run-evaluate.sh proposal-sink.py record-decision.py; do
  cp -f "$SRC_DIR/$f" "$LEDGER_DIR/$f"
  chmod 0700 "$LEDGER_DIR/$f"   # executable: cron invokes run-evaluate.sh directly
done
for f in prompt.md README.md; do
  cp -f "$SRC_DIR/$f" "$LEDGER_DIR/$f"
  chmod 0600 "$LEDGER_DIR/$f"
done
note "copied hook.py, aggregate.py, run-evaluate.sh, proposal-sink.py, prompt.md, README.md into $LEDGER_DIR"

# ----------------------------------------------------------------- backup ----

BACKUP_PATH=""
if [[ -f "$SETTINGS" ]]; then
  BACKUP_PATH="$BACKUP_DIR/settings.json.$(date +%Y%m%d-%H%M%S).bak"
  cp -p "$SETTINGS" "$BACKUP_PATH"
  note "snapshotted settings.json -> $BACKUP_PATH"
else
  printf '{}\n' > "$SETTINGS"
  note "created an empty $SETTINGS (none existed)"
fi

# ------------------------------------------------- register the hook entries --

set +e
HOOK_RESULT="$(
  SETTINGS="$SETTINGS" \
  HOOK_MARKER="$HOOK_MARKER" \
  HOOK_COMMAND="$HOOK_COMMAND" \
  HOOK_TIMEOUT="$HOOK_TIMEOUT" \
  python3 - <<'PY'
import json, os, sys, tempfile

path    = os.environ["SETTINGS"]
marker  = os.environ["HOOK_MARKER"]
command = os.environ["HOOK_COMMAND"]
timeout = int(os.environ["HOOK_TIMEOUT"])
EVENTS  = ("PermissionRequest", "PermissionDenied")

try:
    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read()
    data = json.loads(raw) if raw.strip() else {}
except Exception as exc:                      # noqa: BLE001
    print("FATAL: %s is not readable/valid JSON: %s" % (path, exc), file=sys.stderr)
    sys.exit(1)

if not isinstance(data, dict):
    print("FATAL: %s top level is not an object" % path, file=sys.stderr)
    sys.exit(1)


def strip_ours(hooks, event):
    """Remove every hook entry whose command mentions the marker.

    Drops a matcher group only when the group held nothing but our entries, so
    a group we share with someone else's hook survives with their entry intact.
    """
    groups = hooks.get(event)
    if not isinstance(groups, list):
        return 0
    removed, kept_groups = 0, []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            kept_groups.append(group)
            continue
        original = group["hooks"]
        kept = []
        for entry in original:
            if isinstance(entry, dict) and marker in str(entry.get("command", "")):
                removed += 1
            else:
                kept.append(entry)
        if original and not kept:
            continue                          # group was ours alone -> drop it
        group["hooks"] = kept
        kept_groups.append(group)
    if kept_groups:
        hooks[event] = kept_groups
    else:
        hooks.pop(event, None)
    return removed


hooks = data.get("hooks")
if not isinstance(hooks, dict):
    if hooks is not None:
        print("FATAL: settings.json 'hooks' is not an object", file=sys.stderr)
        sys.exit(1)
    hooks = {}
    data["hooks"] = hooks

before = json.dumps(data, sort_keys=True)
actions = []
for event in EVENTS:
    n = strip_ours(hooks, event)
    hooks.setdefault(event, []).append({
        "matcher": "*",
        "hooks": [{"type": "command", "command": command, "timeout": timeout}],
    })
    actions.append("%s: %s" % (event, "already present, re-registered" if n else "registered"))

if json.dumps(data, sort_keys=True) == before:
    print("SETTINGS_UNCHANGED")
else:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".settings.json.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        try:
            os.chmod(tmp, os.stat(path).st_mode & 0o7777)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    print("SETTINGS_WRITTEN")

for line in actions:
    print("HOOK " + line)
PY
)"
HOOK_STATUS=$?
set -e

if [[ $HOOK_STATUS -ne 0 ]]; then
  echo "install.sh: settings.json edit failed; nothing was written." >&2
  echo "$HOOK_RESULT" >&2
  [[ -n "$BACKUP_PATH" ]] && echo "install.sh: your snapshot is at $BACKUP_PATH" >&2
  exit 1
fi

while IFS= read -r line; do
  case "$line" in
    "HOOK "*)             note "settings.json hooks.${line#HOOK }" ;;
    SETTINGS_UNCHANGED)   note "settings.json already correct — not rewritten" ;;
  esac
done <<< "$HOOK_RESULT"

# ------------------------------------------------------------------- cron ----

if [[ -n "$CRONTAB_CMD" ]]; then
  # Remove-then-append, per rsi-install.sh's convention. Read the whole crontab
  # into a variable FIRST rather than piping `crontab -l` straight into
  # `crontab -`: in the pipeline form the writer's truncation races the reader,
  # which silently eats every other cron line if the writer wins.
  cron_current="$("$CRONTAB_CMD" -l 2>/dev/null || true)"
  cron_kept="$(printf '%s' "$cron_current" | grep -v -F "$CRON_TAG" || true)"
  if [[ -n "$cron_kept" ]]; then
    printf '%s\n%s\n' "$cron_kept" "$CRON_LINE" | "$CRONTAB_CMD" -
  else
    printf '%s\n' "$CRON_LINE" | "$CRONTAB_CMD" -
  fi
  note "crontab: installed the 17:30 run-evaluate.sh line tagged '$CRON_TAG' (via $CRONTAB_CMD)"
else
  note "crontab: SKIPPED — $CRON_SKIP_REASON"
fi

# ---------------------------------------------------------------- summary ----

echo
echo "permission-ledger installed."
for c in "${CHANGES[@]}"; do
  echo "  - $c"
done
echo
echo "Rollback:  bash $SRC_DIR/uninstall.sh"
echo
