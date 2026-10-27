#!/usr/bin/env bash
# Permission Friction Ledger — uninstaller.
#
# Removes the two hook registrations and the run-evaluate.sh cron line. Leaves
# the ledger data, the installed scripts and any pending proposals alone unless
# --purge is given.
#
# Usage:
#   bash uninstall.sh [--purge] [--no-cron]
#
#   --purge    also delete ~/.claude/permission-ledger/ (the ledger .jsonl
#              files, decided.json, digest-latest.json, digest-prev.json,
#              hook-errors.log and the installed scripts: hook.py,
#              aggregate.py, run-evaluate.sh, prompt.md, README.md).
#              Proposals under ~/.claude/proposals/permissions/ are never
#              deleted — they are human-reviewable output, drain them by hand.
#
# Environment:
#   PL_CRONTAB  override the crontab binary. When unset and $HOME does not look
#               like the invoking user's real home, the crontab step is skipped.

set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

TARGET="$HOME/.claude"
LEDGER_DIR="$TARGET/permission-ledger"
BACKUP_DIR="$TARGET/_backups"
SETTINGS="$TARGET/settings.json"

# Matched as a SUBSTRING of the registered command, so an entry registered by an
# older installer (different interpreter flags, e.g. without `-S -E`) is still
# recognised and removed rather than orphaned. Same for the cron tag: the line
# is found by its trailing tag, never by comparing the whole command.
HOOK_MARKER="permission-ledger/hook.py"
CRON_TAG="# permission-ledger-evaluate"

PURGE=false
WANT_CRON=true
for arg in "$@"; do
  case "$arg" in
    --purge)   PURGE=true ;;
    --no-cron) WANT_CRON=false ;;
    -h|--help) sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "uninstall.sh: unknown argument: $arg" >&2; exit 2 ;;
  esac
done

CHANGES=()
note() { CHANGES+=("$1"); }

# --------------------------------------------------- crontab safety decision --

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

# --------------------------------------------- deregister the hook entries ---

if [[ -f "$SETTINGS" ]]; then
  mkdir -p "$BACKUP_DIR"
  BACKUP_PATH="$BACKUP_DIR/settings.json.$(date +%Y%m%d-%H%M%S).bak"
  cp -p "$SETTINGS" "$BACKUP_PATH"
  note "snapshotted settings.json -> $BACKUP_PATH"

  set +e
  HOOK_RESULT="$(
    SETTINGS="$SETTINGS" HOOK_MARKER="$HOOK_MARKER" python3 - <<'PY'
import json, os, sys, tempfile

path   = os.environ["SETTINGS"]
marker = os.environ["HOOK_MARKER"]
EVENTS = ("PermissionRequest", "PermissionDenied")

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

hooks = data.get("hooks")
if not isinstance(hooks, dict):
    print("REMOVED 0")
    sys.exit(0)


def strip_ours(event):
    groups = hooks.get(event)
    if not isinstance(groups, list):
        return 0
    removed, kept_groups = 0, []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            kept_groups.append(group)
            continue
        original = group["hooks"]
        kept = [e for e in original
                if not (isinstance(e, dict) and marker in str(e.get("command", "")))]
        removed += len(original) - len(kept)
        if original and not kept:
            continue                          # group was ours alone -> drop it
        group["hooks"] = kept
        kept_groups.append(group)
    if kept_groups:
        hooks[event] = kept_groups
    else:
        hooks.pop(event, None)
    return removed


# Deliberately never pops the top-level "hooks" key, even if it ends up empty:
# an uninstaller must not change the top-level key set of settings.json.
total = sum(strip_ours(e) for e in EVENTS)

if total:
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

print("REMOVED %d" % total)
PY
  )"
  HOOK_STATUS=$?
  set -e

  if [[ $HOOK_STATUS -ne 0 ]]; then
    echo "uninstall.sh: settings.json edit failed; nothing was written." >&2
    echo "$HOOK_RESULT" >&2
    exit 1
  fi
  note "settings.json: removed ${HOOK_RESULT#REMOVED } permission-ledger hook entr(y/ies)"
else
  note "settings.json: not present, nothing to deregister"
fi

# ------------------------------------------------------------------- cron ----

if [[ -n "$CRONTAB_CMD" ]]; then
  # Read fully before writing — see the note in install.sh; piping `crontab -l`
  # directly into `crontab -` races truncation against the read.
  cron_current="$("$CRONTAB_CMD" -l 2>/dev/null || true)"
  cron_kept="$(printf '%s' "$cron_current" | grep -v -F "$CRON_TAG" || true)"
  if [[ -n "$cron_kept" ]]; then
    printf '%s\n' "$cron_kept" | "$CRONTAB_CMD" -
  else
    printf '' | "$CRONTAB_CMD" -
  fi
  note "crontab: removed any line tagged '$CRON_TAG' (via $CRONTAB_CMD)"
else
  note "crontab: SKIPPED — $CRON_SKIP_REASON"
fi

# ------------------------------------------------------------------ purge ----

if [[ "$PURGE" == true ]]; then
  if [[ -d "$LEDGER_DIR" ]]; then
    rm -rf "$LEDGER_DIR"
    # Scripts only. The ledger, decided.json and the digests live in the state
    # directory and are NOT touched here, so --purge is not a data erase.
    note "purged $LEDGER_DIR (installed scripts)"
    note "ledger data left in ${PERMISSION_LEDGER_DIR:-$HOME/.local/state/permission-ledger} — delete it by hand if you mean to"
  else
    note "purge requested but $LEDGER_DIR does not exist"
  fi
else
  note "left $LEDGER_DIR in place (pass --purge to delete it)"
fi
note "left ~/.claude/proposals/permissions/ untouched"

# ---------------------------------------------------------------- summary ----

echo
echo "permission-ledger uninstalled."
for c in "${CHANGES[@]}"; do
  echo "  - $c"
done
echo
echo "Reinstall: bash $SRC_DIR/install.sh"
echo
