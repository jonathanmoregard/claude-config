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
#   * It never destroys work the sink's git history does not already hold.
#     Every check that can refuse runs BEFORE the first mutation: the sink
#     must be a repo of its own, outside ~/.claude, with a clean worktree, and
#     no file may exist in both trees with different bytes. (Until 2026-08-31
#     the clean-worktree check sat after the sync AND after the commit, and the
#     sync resolved a difference by overwriting the sink with the ~/.claude
#     copy — so an uncommitted sink edit was destroyed and landed in no commit
#     anywhere. Do not move these checks back down.)
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

SINK_REAL="$(cd "$SINK" && pwd -P)"
CLAUDE_REAL="$(cd "$CLAUDE_HOME" 2>/dev/null && pwd -P || printf '%s' "$CLAUDE_HOME")"

# CONTAINMENT. The whole point of the sink is that it is NOT inside the live
# config checkout. Nothing downstream asserted that, so a `proposals.folder`
# still carrying the pre-move `~/.claude/proposals` sailed through preflight —
# it is, after all, inside *a* git repo. What followed was `git -C $SINK add
# -A && commit` sweeping unrelated WIP into a commit on the live config repo,
# and a `proposals -> proposals` symlink loop with the content in the backup.
if [[ "$SINK_REAL" == "$CLAUDE_REAL" || "$SINK_REAL" == "$CLAUDE_REAL"/* ]]; then
  die "the proposals sink $SINK resolves INSIDE the live config checkout $CLAUDE_HOME. That is the layout this script exists to undo, not one it can cut over to: committing there dirties the tree the harness reads, and the swap would point the link at itself. Fix recursive-self-improvement/config/config.json -> proposals.folder (it should name a path outside ~/.claude, canonically $SINK_DEFAULT). Nothing was changed."
fi

# The sink must BE a git repo, not merely sit inside one. `rev-parse
# --show-toplevel` answers about the ENCLOSING repo, so this is the check that
# actually holds: every `git add -A` below is aimed at $SINK, and staging a
# repo whose root is somewhere else stages that repo's entire worktree.
SINK_TOP="$(git -C "$SINK" rev-parse --show-toplevel 2>/dev/null || true)"
[[ -n "$SINK_TOP" ]] \
  || die "the proposals sink $SINK is not a git repo. Content copied into it could not be committed, and rejected proposals would stop being greppable with 'git log -S'. Refusing to swap anything."
if [[ "$(cd "$SINK_TOP" && pwd -P)" != "$SINK_REAL" ]]; then
  die "the proposals sink $SINK is not a git repo of its own — it sits inside the repo rooted at $SINK_TOP. 'git add -A' here would stage that repo's whole worktree, not the sink's. Run 'git -C $SINK init' (or repoint proposals.folder). Nothing was changed."
fi

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

# ------------------------------------------------------- clean sink gate ----
# THIS RUNS BEFORE THE FIRST MUTATION, AND MUST STAY HERE. The sync below is
# only safe because everything it can touch is already in the sink's git
# history; an uncommitted edit is not, and until 2026-08-31 this check sat
# after the sync and after the commit, i.e. exactly where it could no longer
# help. Measured: a sink-only edit showing ` M` in git status came back as the
# ~/.claude version, and `git log -S` found the edit in no commit anywhere.
if [[ -n "$(git -C "$SINK" status --porcelain)" ]]; then
  echo "error: the sink $SINK has uncommitted changes:" >&2
  git -C "$SINK" status --porcelain >&2
  echo "Commit them before the cutover. Nothing this script retires is recoverable unless the sink's history holds it, and nothing was changed." >&2
  exit 1
fi

# ------------------------------------------------- sync live -> sink first ---
# Never delete a file the sink does not already hold. Anything present live and
# missing from the sink is copied in and committed there, so the swap can only
# ever remove a directory whose every byte already lives in the sink's history.
#
# A file present in BOTH with different bytes is NOT resolved. It used to be:
# the ~/.claude copy won, on the reasoning that the sink's version stayed in
# git history — true only while the sink is clean, and false in the one case
# that mattered. Two versions of the same proposal is a fact about the world
# that a relocation script has no standing to adjudicate, so it prints the
# diff and stops.

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

# `-xtype f` rather than `-type f`. `-type` lstats, so a symlinked .md in the
# live tree matched nothing: it was neither copied into the sink nor looked at
# by the verify pass, and it simply stopped existing in the spine at the swap,
# with no log line and the content surviving only in the backup directory.
# `-xtype f` matches regular files AND symlinks resolving to regular files,
# while still not matching the `proposals/rsi` directory symlink (find does not
# descend into it either, which is what keeps rsi/rsi out of the sink).
live_files() {  # <live dir> -> NUL-separated paths
  find "$1" -xtype f -print0
}

DIV_REL=(); DIV_SRC=(); DIV_DST=(); ESCAPED=()

# Pass 1. Look at everything and decide, mutating nothing — a half-copied sink
# plus an abort is a worse state than either outcome on its own.
scan_conflicts() {  # <live dir> <sink dir>
  local live="$1" dest="$2" rel src dst real
  [[ -d "$live" && ! -L "$live" ]] || return 0
  while IFS= read -r -d '' src; do
    if skip_path "$src"; then
      continue
    fi
    if [[ -L "$src" ]]; then
      # Proposals are written by unattended agents. Following a link out of
      # $HOME would copy whatever it names into a git repo and commit it.
      real="$(readlink -f -- "$src" 2>/dev/null || true)"
      case "$real" in
        "$HOME"/*) ;;
        *) ESCAPED+=("$src -> ${real:-<unresolvable>}"); continue ;;
      esac
    fi
    rel="${src#"$live"/}"
    dst="$dest/$rel"
    if [[ ! -e "$dst" ]] || cmp -s -- "$src" "$dst"; then
      continue
    fi
    DIV_REL+=("$rel"); DIV_SRC+=("$src"); DIV_DST+=("$dst")
  done < <(live_files "$live")
  while IFS= read -r -d '' src; do
    echo "cutover: note — $src is a broken symlink; it has no content to preserve and will not be recreated in the sink" >&2
  done < <(find "$live" -xtype l -print0)
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
    # Additions only. Pass 1 proved nothing here diverges, and an escaping
    # symlink was refused there.
    if [[ -e "$dst" ]]; then
      continue
    fi
    echo "cutover: copied into the sink: ${dst#"$SINK"/}"
    mkdir -p -- "$(dirname -- "$dst")"
    # -L: a symlinked proposal lands in the sink as its content. A copy of the
    # pointer would break the day the backup directory is deleted.
    cp -p -L -- "$src" "$dst"
  done < <(live_files "$live")
}

for pair in "${LINKS[@]}"; do
  scan_conflicts "${pair%%:*}" "${pair#*:}"
done

if [[ "${#ESCAPED[@]}" -gt 0 ]]; then
  echo "error: these live proposal entries are symlinks resolving outside $HOME:" >&2
  for e in "${ESCAPED[@]}"; do printf '  %s\n' "$e" >&2; done
  echo "Absorbing them would copy content the spine never owned into a git repo. Remove or repoint them, then re-run. Nothing was changed." >&2
  exit 1
fi

if [[ "${#DIV_REL[@]}" -gt 0 ]]; then
  echo "error: these files exist in BOTH trees with different bytes, and this script does not get to choose between them:" >&2
  for i in "${!DIV_REL[@]}"; do
    printf '\n  %s\n' "${DIV_REL[$i]}" >&2
    diff -u -- "${DIV_DST[$i]}" "${DIV_SRC[$i]}" >&2 || true
  done
  echo "" >&2
  echo "Reconcile them by hand (the sink copy is the '---' side, the ~/.claude copy the '+++' side), commit the result in $SINK, then re-run. Nothing was changed." >&2
  exit 1
fi

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
  done < <(live_files "$live")
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

# The clean-sink gate is UPSTREAM of the sync now (see the top of this file),
# so by here the only thing that could have dirtied the sink is this script's
# own commit above. Belt and braces: a residual difference means something
# outside this script wrote to the sink mid-run.
if [[ -n "$(git -C "$SINK" status --porcelain)" ]]; then
  die "the sink $SINK went dirty during the cutover — another writer is active. Nothing was moved, nothing was removed."
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
