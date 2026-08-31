#!/usr/bin/env bash
# Finish the proposals relocation: replace the two live directories in
# ~/.claude with absolute symlinks into the sink.
#
# WHY THIS IS A SEPARATE SCRIPT AND NOT PART OF THE PR.
# Replacing a tracked directory with a symlink reads to git as 54 deletions
# plus one new symlink object. So the untracking and the .gitignore rules have
# to be on master FIRST; only then can the swap happen without the live
# checkout going dirty. This script is the "only then" half, and it refuses to
# run before the merge.
#
# WHAT IT GUARANTEES.
#   * It never deletes proposal content. The live directories are MOVED to a
#     timestamped backup outside ~/.claude, never removed.
#   * It never swaps anything it has not first proven is safe: every file in
#     the live tree must exist in the sink, byte-identical. Content the sink
#     lacks is copied in and committed there BEFORE the swap; if anything is
#     still not identical afterwards, the script aborts and touches nothing.
#   * It is idempotent: an already-correct symlink is a no-op, exit 0.
#
# Everything is derived from $HOME and from
# recursive-self-improvement/config/config.json -> proposals.folder, which is
# the single source of truth for the sink path. Symlink targets are absolute,
# matching the style of the symlink this replaces.
#
# Usage:  ~/.claude/scripts/cutover-proposals.sh
set -euo pipefail

CLAUDE_HOME="$HOME/.claude"
RSI_CONFIG="$CLAUDE_HOME/recursive-self-improvement/config/config.json"
SINK_DEFAULT="$HOME/.local/state/claude-proposals"
BACKUP_ROOT="$HOME/.local/state/claude-proposals-backup"

resolve_sink() {
  python3 -S -E - "$RSI_CONFIG" "$SINK_DEFAULT" <<'PY' 2>/dev/null || printf '%s\n' "$SINK_DEFAULT"
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
SINK="$(resolve_sink)"

die() { echo "error: $*" >&2; exit 1; }

# link path : sink path it must point at
LINKS=(
  "$CLAUDE_HOME/proposals:$SINK"
  "$CLAUDE_HOME/recursive-self-improvement/proposals:$SINK/rsi"
)

# ------------------------------------------------------------- preflight ----

[[ -d "$SINK" ]] || die "the proposals sink $SINK does not exist — build it before cutting over."
git -C "$SINK" rev-parse --show-toplevel >/dev/null 2>&1 \
  || die "the proposals sink $SINK is not a git repo. Content copied into it could not be committed, and rejected proposals would stop being greppable with 'git log -S'. Refusing to swap anything."
[[ -d "$SINK/rsi" ]] || die "$SINK/rsi is missing — the sink is not laid out for the cutover."

# Already done? Idempotence is checked before anything else so a re-run is
# cheap and silent-ish rather than re-verifying the whole tree.
all_linked=1
for pair in "${LINKS[@]}"; do
  link="${pair%%:*}"; want="${pair#*:}"
  if [[ -L "$link" && "$(readlink -- "$link")" == "$want" ]]; then
    continue
  fi
  all_linked=0
done
if [[ "$all_linked" == 1 ]]; then
  echo "cutover: already done — both names are absolute symlinks into $SINK; nothing to do."
  exit 0
fi

# The merge gate. While git still tracks these paths, swapping in a symlink
# shows up as a mass deletion in the live checkout.
if git -C "$CLAUDE_HOME" rev-parse --show-toplevel >/dev/null 2>&1; then
  tracked="$(git -C "$CLAUDE_HOME" ls-files -- proposals recursive-self-improvement/proposals | head -5)"
  if [[ -n "$tracked" ]]; then
    echo "error: git still tracks the proposal paths in $CLAUDE_HOME, e.g.:" >&2
    while IFS= read -r t; do printf '  %s\n' "$t" >&2; done <<<"$tracked"
    echo "The PR that untracks them and adds the .gitignore rules must merge first — otherwise this swap reads as a mass deletion in the live checkout. Nothing was changed." >&2
    exit 1
  fi
fi

# ------------------------------------------------- sync live -> sink first ---
# Never delete a file the sink does not already hold. Anything present live and
# missing (or different) in the sink is copied in and committed there, so the
# swap can only ever remove a directory whose every byte already lives in the
# sink's history.

# `.gitignore` is repo plumbing, not proposal content, and the sink's own
# ignore policy is deliberately the OPPOSITE of the legacy one: out here
# everything is tracked, including gate-log.jsonl and permissions/, which were
# only ever ignored back in ~/.claude because each run dirtied the live config
# tree. Copying a legacy .gitignore over it would silently un-track the very
# content the move exists to preserve. Same for anything inside a .git dir.
#
# gate-config.json is skipped for a different reason: it is spine CONFIG, and
# three of its keys are invalidated by the move itself. It gets one writer —
# install_gate_config below — rather than being copied and then rewritten.
skip_path() {  # <absolute path> -> 0 = skip
  case "$1" in
    */.git/*) return 0 ;;
  esac
  case "$(basename -- "$1")" in
    .gitignore|gate-config.json) return 0 ;;
  esac
  return 1
}

# The relocation invalidates exactly three keys in the spine's gate config, and
# leaving any of them stale is a silent failure rather than a loud one:
#
#   spine_root / gate_log — would keep pointing at ~/.claude/proposals. That is
#     now a symlink to the sink, so it happens to still resolve; but a consumer
#     that depends on a symlink existing fails silently the day it does not.
#   on_decision.permissions — the real trap. proposal-intake-gate.py refuses
#     any callback whose realpath escapes ~/.claude (a security boundary: a
#     proposal file must never get to nominate an arbitrary executable). A
#     callback under the spine now resolves into ~/.local/state, so the
#     permission-ledger decision drain would die on a bare exit 1, and every
#     drained pattern would come back in tomorrow's digest forever.
#
# Everything else in the file is the user's and is preserved untouched. The
# patch is idempotent.
install_gate_config() {
  local live="$CLAUDE_HOME/proposals/gate-config.json"
  local dst="$SINK/gate-config.json"
  if [[ ! -f "$dst" && -f "$live" && ! -L "$CLAUDE_HOME/proposals" ]]; then
    cp -p -- "$live" "$dst"
    echo "cutover: seeded $dst from the ~/.claude copy"
  fi
  python3 -S -E - "$dst" "$SINK" "$CLAUDE_HOME/permission-ledger/on-decision" <<'PY'
import json
import os
import sys

path, sink, callback = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    if not isinstance(cfg, dict):
        raise ValueError("gate-config.json is not a JSON object")
except FileNotFoundError:
    cfg = {}
changed = []
for key, want in (("spine_root", sink),
                  ("gate_log", sink + "/gate-log.jsonl")):
    if cfg.get(key) != want:
        cfg[key] = want
        changed.append(key)
on_decision = cfg.get("on_decision")
if not isinstance(on_decision, dict):
    on_decision = {}
if on_decision.get("permissions") != callback:
    on_decision["permissions"] = callback
    changed.append("on_decision.permissions")
cfg["on_decision"] = on_decision
if changed:
    tmp = path + ".cutover-tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    print("cutover: gate-config.json updated: " + ", ".join(changed))
else:
    print("cutover: gate-config.json already points at the sink")
PY
}

sync_into_sink() {  # <live dir> <sink dir>
  local live="$1" dest="$2" rel src dst
  [[ -d "$live" && ! -L "$live" ]] || return 0
  while IFS= read -r -d '' src; do
    if skip_path "$src"; then
      echo "cutover: skipped $src (repo plumbing — the sink keeps its own)"
      continue
    fi
    rel="${src#"$live"/}"
    dst="$dest/$rel"
    if [[ -f "$dst" ]] && cmp -s -- "$src" "$dst"; then
      continue
    fi
    # The live copy wins on a conflict, and that is safe rather than
    # destructive: the sink is a git repo whose current version is committed
    # (verified below), so the overwritten bytes stay in its history.
    if [[ -e "$dst" ]]; then
      echo "cutover: sink copy of $rel differs — taking the ~/.claude version (the previous one stays in the sink's git history)"
    else
      echo "cutover: copied into the sink: ${dst#"$SINK"/}"
    fi
    mkdir -p -- "$(dirname -- "$dst")"
    cp -p -- "$src" "$dst"
  done < <(find "$live" -type f -print0)
}

for pair in "${LINKS[@]}"; do
  sync_into_sink "${pair%%:*}" "${pair#*:}"
done
install_gate_config

git -C "$SINK" add -A
if git -C "$SINK" diff --cached --quiet; then
  echo "cutover: sink already held every byte; nothing to commit"
else
  git -C "$SINK" commit -q -m "cutover: absorb proposal content and repoint gate-config at the sink"
  echo "cutover: committed the absorbed content in $SINK"
fi

# ------------------------------------------------------------- verify --------
# Hard gate. After the sync every live file must exist in the sink and be
# byte-identical; anything else means the sync could not do its job (a failed
# copy, an unreadable file, a sink that would not commit) and we must not
# remove the live tree.

verify() {  # <live dir> <sink dir>
  local live="$1" dest="$2" rel src dst rc=0
  [[ -d "$live" && ! -L "$live" ]] || return 0
  while IFS= read -r -d '' src; do
    if skip_path "$src"; then continue; fi
    rel="${src#"$live"/}"
    dst="$dest/$rel"
    if [[ ! -f "$dst" ]]; then
      echo "  missing in the sink: $rel" >&2; rc=1
    elif ! cmp -s -- "$src" "$dst"; then
      echo "  differs from the sink: $rel" >&2; rc=1
    fi
  done < <(find "$live" -type f -print0)
  return "$rc"
}

verify_failed=0
for pair in "${LINKS[@]}"; do
  verify "${pair%%:*}" "${pair#*:}" || verify_failed=1
done
if [[ "$verify_failed" == 1 ]]; then
  echo "error: the sink does not hold every live file byte-for-byte (listed above). Nothing was moved, nothing was removed, no symlink was created. Fix the sink, then re-run." >&2
  exit 1
fi

# Uncommitted content in the sink would not be recoverable by git after the
# live copy is retired, which is the whole point of the sink being a repo.
if [[ -n "$(git -C "$SINK" status --porcelain)" ]]; then
  die "the sink $SINK has uncommitted changes — commit them before the cutover so nothing retired here is unrecoverable. Nothing was changed."
fi

# ------------------------------------------------------------- the swap -----

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="$BACKUP_ROOT/$STAMP"

for pair in "${LINKS[@]}"; do
  link="${pair%%:*}"; want="${pair#*:}"

  if [[ -L "$link" ]]; then
    current="$(readlink -- "$link")"
    if [[ "$current" == "$want" ]]; then
      echo "cutover: $link already points at $want — left alone"
      continue
    fi
    rm -- "$link"   # a symlink is a pointer, not content: removing it loses nothing
    echo "cutover: retargeted $link (was -> $current)"
  elif [[ -e "$link" ]]; then
    mkdir -p -- "$BACKUP"
    parent="$(basename -- "$(dirname -- "$link")")"
    dest="$BACKUP/${parent#.}-$(basename -- "$link")"   # no leading dot: the backup should not be hidden
    mv -- "$link" "$dest"
    echo "cutover: moved $link -> $dest (preserved, not deleted)"
  fi

  ln -s -- "$want" "$link"
  echo "cutover: $link -> $want"
done

echo "cutover: done. Spine: $SINK"
if [[ -d "$BACKUP" ]]; then
  echo "cutover: the replaced directories are preserved at $BACKUP — delete them yourself once you are satisfied."
fi
