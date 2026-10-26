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
# THE ENUMERATION IS CLOSED, AND UNPLACEABLE INPUT ABORTS LOUDLY.
# This script used to enumerate with `find` predicates that SELECT — `-xtype f`
# for content, `-xtype l` for broken links — and to check properties on one
# side of a src/dst pair but not the other. Both are the same mistake wearing
# two hats, and eight reproduced defects came out of it:
#
#   * A node matching NEITHER predicate was invisible to all three passes at
#     once. A symlink to a directory is the worked example: `-xtype f` does not
#     match it, `-xtype l` does not match it, and find does not descend into
#     it, so it was never copied, never verified, never even logged — it simply
#     stopped existing in the spine at the swap, at exit 0, with the body left
#     in a backup directory the closing message invites you to delete. FIFOs
#     went the same way. Each new node type would have needed its own new
#     predicate, discovered the hard way, one data-loss report at a time.
#   * A pair whose LIVE side looked fine could have a sink side that was not.
#     A dangling sink symlink made `[[ -e $dst ]]` false, so `cp` wrote through
#     it and coreutils refused mid-sync; a sink symlink pointing back into the
#     live tree made `-e` true and made verify's `cmp` compare the live file
#     with ITSELF, so the run passed and the swap then turned the path into an
#     ELOOP with the only real copy in the backup.
#
# So the enumeration no longer selects. `find -mindepth 1` yields EVERY node,
# and each one is classified into a closed set — content, directory, directory
# symlink inside the trees, pointer with nothing behind it — with a final arm
# that REFUSES. The next node type nobody thought of fails closed and names
# itself, instead of disappearing quietly. Both sides of every pair are
# classified, not just the live one.
#
# Three consequences worth keeping:
#   * The classification runs ONCE, in a pass that mutates nothing, and the
#     sync and the verify are driven from its result. Three walks that could
#     disagree about what exists were the substrate the bugs above grew in.
#   * Every refusal therefore lands BEFORE the first byte is written. A refusal
#     raised halfway through the sync leaves the sink dirty, and the clean-sink
#     gate then blocks every later run until a human intervenes — the worst
#     outcome available here, worse than either finishing or not starting.
#   * A copy can still fail for reasons no preflight can rule out (ENOSPC, EIO,
#     a writer removing a file between the plan and the copy), so the sync also
#     unwinds its own partial writes on the way out.
#
# WHAT A SYMLINK IS ALLOWED TO NAME.
# The old rule was "anywhere under $HOME", which admits ~/.ssh/id_ed25519 —
# measured: copied by content and committed into the sink at exit 0. The rule
# is now the two live proposal trees and nothing else, judged on the FULLY
# resolved target, so a chain that hops out and back in is fine and one that
# ends outside is not. That is strictly narrower than the check it replaces and
# still refuses everything the $HOME check refused; the case the $HOME check
# existed to allow — a proposal symlinked to another proposal, including
# relative links and the production `proposals/rsi` directory link — is
# entirely inside the trees and still works.
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

# SERIALISATION. Two cutovers at once used to interleave and one of them died
# mid-run: the second copied a file the first had already committed (`cp:
# File exists`), or reached the swap after the first had moved the tree away
# (`mv: cannot stat`). Nothing was lost either time, but the operation is not
# atomic and cannot be made so, so the runs are serialised instead. Unlike a
# lock the unattended proposal WRITERS would have to opt into — which would be
# theatre, since they do not take it — both racers here run this very script,
# so this lock is actually held by everyone it has to exclude.
command -v flock >/dev/null 2>&1 \
  || die "flock (util-linux) is not on PATH. It is what keeps two cutovers from interleaving, and running without it is not an option this script offers. Nothing was changed."
LOCK_FILE="${SINK%/}.cutover.lock"
exec 9>"$LOCK_FILE" || die "cannot open the cutover lock $LOCK_FILE"
flock -w 300 9 \
  || die "another cutover is holding $LOCK_FILE and did not finish within 300s. Nothing was changed."

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

# ...and its OBJECT STORE must be its own too. A git WORKTREE of the ~/.claude
# repo passed both checks above — it resolves outside ~/.claude, and
# `--show-toplevel` answers with the worktree's own root — while every commit
# made in it lands in the PUBLIC config repo's history. Measured: `git add -A`
# swept the checkout and committed proposal content there, exit 0. The two
# containment questions are "where is the worktree" and "where is the object
# store", and only the second one catches this.
SINK_GITDIR="$(git -C "$SINK" rev-parse --git-common-dir 2>/dev/null || true)"
case "$SINK_GITDIR" in
  "") SINK_GITDIR="$SINK/.git" ;;
  /*) ;;
  *)  SINK_GITDIR="$SINK/$SINK_GITDIR" ;;
esac
SINK_GITDIR_REAL="$(cd "$SINK_GITDIR" 2>/dev/null && pwd -P || printf '%s' "$SINK_GITDIR")"
if [[ "$SINK_GITDIR_REAL" != "$SINK_REAL"/* && "$SINK_GITDIR_REAL" != "$SINK_REAL" ]]; then
  die "the proposals sink $SINK keeps its git objects in $SINK_GITDIR_REAL, outside the sink — it is a worktree (or a --separate-git-dir checkout) of another repository, not a repository of its own. Every commit this script makes would land in THAT repo's history; when it is the ~/.claude config repo, proposal content ends up published. Give the sink its own repo ('git -C $SINK init') or repoint proposals.folder. Nothing was changed."
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

# --------------------------------------------------------- backup preflight --
# The backup is the LAST copy of the directories this script retires, so it
# gets the same rigour as the sink, and it gets it HERE — an unwritable backup
# root used to be discovered by `mkdir` after the sink had already been
# committed to, and a symlinked backup root was followed in silence, writing
# the last copy somewhere the user never named.
if [[ -L "$BACKUP_ROOT" ]]; then
  die "the backup root $BACKUP_ROOT is a symlink -> $(readlink -- "$BACKUP_ROOT"). The directories this script retires are preserved there and nowhere else; following a link to somewhere you did not name is how that copy gets lost. Replace it with a real directory, or remove it and let this script create one. Nothing was changed."
fi
if [[ -e "$BACKUP_ROOT" ]]; then
  [[ -d "$BACKUP_ROOT" ]] \
    || die "the backup root $BACKUP_ROOT exists and is not a directory, so the retired trees have nowhere to go. Nothing was changed."
  [[ -w "$BACKUP_ROOT" && -x "$BACKUP_ROOT" ]] \
    || die "the backup root $BACKUP_ROOT is not writable, so the retired trees would have nowhere to go. This used to be discovered by mkdir AFTER the sink had been committed to. Fix its permissions, then re-run. Nothing was changed."
else
  probe="$(dirname -- "$BACKUP_ROOT")"
  while [[ ! -e "$probe" && "$probe" != "/" ]]; do probe="$(dirname -- "$probe")"; done
  [[ -d "$probe" && -w "$probe" && -x "$probe" ]] \
    || die "the backup root $BACKUP_ROOT cannot be created: $probe is not a writable directory. Nothing was changed."
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
    */.git|*/.git/*) return 0 ;;
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
  if [[ ! -f "$dst" ]]; then
    # Whether it is seeded from the ~/.claude copy here or written from
    # scratch by the patch below, this run is the one that created it.
    CREATED_FILES+=("$dst")
    if [[ -f "$live" && ! -L "$CLAUDE_HOME/proposals" ]]; then
      cp -p -- "$live" "$dst"
      echo "cutover: seeded $dst from the ~/.claude copy"
    fi
  else
    # Rewritten in place below. The clean-sink gate proved it is committed, so
    # `git checkout HEAD --` can put it back if anything downstream aborts.
    RESTORE_FILES+=("$dst")
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

# ------------------------------------------------------- node classification -

# The canonical roots of the two live proposal trees. Canonical, but WITHOUT
# following a symlink standing at the root itself — that case is judged
# separately by classify_targets, and resolving it here would silently widen
# the set of paths a proposal symlink is allowed to name.
tree_root_of() {  # <link path> -> canonical path
  local parent base real
  parent="$(dirname -- "$1")"; base="$(basename -- "$1")"
  real="$(cd "$parent" 2>/dev/null && pwd -P || printf '%s' "$parent")"
  printf '%s/%s\n' "$real" "$base"
}
TREE_ROOTS=()
for pair in "${LINKS[@]}"; do TREE_ROOTS+=("$(tree_root_of "${pair%%:*}")"); done

inside_trees() {  # <fully resolved path> -> 0 = inside a live proposal tree
  local p="$1" root
  for root in "${TREE_ROOTS[@]}"; do
    [[ "$p" == "$root" || "$p" == "$root"/* ]] && return 0
  done
  return 1
}

node_kind() {  # <path> -> a human name for a thing that is not a file or a dir
  if   [[ -p "$1" ]]; then printf 'a named pipe (FIFO)'
  elif [[ -S "$1" ]]; then printf 'a socket'
  elif [[ -b "$1" ]]; then printf 'a block device'
  elif [[ -c "$1" ]]; then printf 'a character device'
  else                     printf 'not a regular file'
  fi
}

REFUSE=(); NOTES=()
DIV_REL=(); DIV_SRC=(); DIV_DST=()
COPY_SRC=(); COPY_DST=(); COPY_REL=()
VER_SRC=(); VER_DST=(); VER_REL=()
declare -A PLANNED=()

# Pass 1. Look at EVERY node and decide, mutating nothing — a half-copied sink
# plus an abort is a worse state than either outcome on its own, and it wedges
# every later run behind the clean-sink gate.
plan_tree() {  # <live dir> <sink dir>
  local live="$1" dest="$2" src rel dst real
  while IFS= read -r -d '' src; do
    if skip_path "$src"; then
      case "$src" in
        */.git|*/.git/*) ;;
        *) echo "cutover: skipped $src (repo plumbing — the sink keeps its own)" ;;
      esac
      continue
    fi
    rel="${src#"$live"/}"
    dst="$dest/$rel"
    PLANNED["$dst"]=1

    if [[ -L "$src" ]]; then
      real="$(readlink -f -- "$src" 2>/dev/null || true)"
      if [[ -z "$real" || ! -e "$real" ]]; then
        NOTES+=("$src is a broken symlink; it has no content to preserve and will not be recreated in the sink")
        continue
      fi
      if ! inside_trees "$real"; then
        REFUSE+=("$src -> $real
      a symlink resolving OUTSIDE the two proposal trees. Proposals are written
      by unattended agents, and absorbing this would copy content the spine
      never owned into a git repo and commit it — \$HOME is where the
      credentials live, so \"inside \$HOME\" was never the boundary. Remove the
      link or repoint it inside the trees.")
        continue
      fi
      if [[ -d "$real" ]]; then
        NOTES+=("$src -> $real is a directory symlink inside the proposal trees; its target is absorbed in its own right, so the link itself is not recreated in the sink")
        continue
      fi
      if [[ ! -f "$real" ]]; then
        REFUSE+=("$src -> $real
      a symlink to $(node_kind "$real"). The sink can hold regular files and
      directories; there is nothing here the cutover can copy.")
        continue
      fi
      # falls through: a symlink to a regular file inside the trees is content
    elif [[ -d "$src" ]]; then
      if [[ -L "$dst" ]]; then
        REFUSE+=("$rel
      the live tree has a directory here; the sink has a SYMLINK -> $(readlink -- "$dst").
      Copying into it would write through the link, out of the sink.")
      elif [[ -e "$dst" && ! -d "$dst" ]]; then
        REFUSE+=("$rel
      the live tree has a directory here; the sink has a file. This used to
      blow up mid-sync with a bare \"mkdir: File exists\" and leave the sink
      dirty. Reconcile the two by hand, commit in the sink, then re-run.")
      fi
      continue
    elif [[ ! -f "$src" ]]; then
      REFUSE+=("$src
      $(node_kind "$src") — the cutover can place regular files, directories,
      and symlinks to either, and nothing else. It used to drop this silently:
      no copy, no verify, no log line, and the name simply gone from the spine
      after the swap.")
      continue
    fi

    # -- a content node: a regular file, or a symlink resolving to one --------
    if [[ ! -r "$src" ]]; then
      REFUSE+=("$src
      unreadable. The copy would fail mid-sync and leave the sink dirty, which
      the clean-sink gate then turns into a block on every later run.")
      continue
    fi
    if [[ -L "$dst" ]]; then
      REFUSE+=("$rel
      the SINK side of this path is a symlink -> $(readlink -- "$dst").
      Only the live side was ever tested for that: a dangling one made cp write
      through the sink to somewhere else, and one pointing back into the live
      tree made verify compare the live file with ITSELF and pass, leaving an
      ELOOP where the proposal used to be. Replace it with real content in the
      sink, commit, then re-run.")
      continue
    fi
    if [[ -e "$dst" ]]; then
      if [[ ! -f "$dst" ]]; then
        REFUSE+=("$rel
      the live tree has a file here; the sink has $(node_kind "$dst"). Neither
      side can be turned into the other automatically.")
        continue
      fi
      if ! cmp -s -- "$src" "$dst"; then
        DIV_REL+=("$rel"); DIV_SRC+=("$src"); DIV_DST+=("$dst")
        continue
      fi
      VER_SRC+=("$src"); VER_DST+=("$dst"); VER_REL+=("$rel")
      continue
    fi
    COPY_SRC+=("$src"); COPY_DST+=("$dst"); COPY_REL+=("$rel")
    VER_SRC+=("$src"); VER_DST+=("$dst"); VER_REL+=("$rel")
  done < <(find "$live" -mindepth 1 -print0)
}

# The TARGET paths themselves get classified too. `[[ -d $live && ! -L $live ]]`
# made scan, sync and verify all `return 0` when one of them was already a
# symlink, and the swap then `rm`'d the pointer: real content behind it reached
# neither the sink nor the backup, at exit 0.
TARGET_STATE=()
classify_targets() {
  local i pair link want real want_real
  for i in "${!LINKS[@]}"; do
    pair="${LINKS[$i]}"; link="${pair%%:*}"; want="${pair#*:}"
    if [[ -L "$link" ]]; then
      if [[ "$(readlink -- "$link")" == "$want" ]]; then
        TARGET_STATE[$i]=linked; continue
      fi
      real="$(readlink -f -- "$link" 2>/dev/null || true)"
      want_real="$(readlink -f -- "$want" 2>/dev/null || true)"
      if [[ -z "$real" || ! -e "$real" \
            || ( -n "$want_real" && "$real" == "$want_real" ) ]]; then
        # Dangling, self-looping, or already naming the sink by another
        # spelling: a pointer with nothing behind it loses nothing when it is
        # replaced by the right pointer.
        TARGET_STATE[$i]=drop; continue
      fi
      REFUSE+=("$link -> $real
      the target path is ALREADY a symlink, and there is real content behind
      it. The swap only ever removes a pointer — it never follows one — so that
      content would reach neither the sink nor the backup, and the run would
      still say \"done\". Move $real into the sink yourself (or delete the link
      if you know it is worthless), then re-run.")
      TARGET_STATE[$i]=refused; continue
    fi
    if [[ ! -e "$link" ]]; then TARGET_STATE[$i]=absent; continue; fi
    if [[ -d "$link" ]]; then TARGET_STATE[$i]=dir; continue; fi
    REFUSE+=("$link
      the target path exists and is $(node_kind "$link"). The cutover replaces
      a proposal DIRECTORY with a symlink; it will not guess what to do with
      this.")
    TARGET_STATE[$i]=refused
  done
}

classify_targets
for i in "${!LINKS[@]}"; do
  if [[ "${TARGET_STATE[$i]}" == dir ]]; then
    pair="${LINKS[$i]}"
    plan_tree "${pair%%:*}" "${pair#*:}"
  fi
done

for ((i=0; i<${#NOTES[@]}; i++)); do
  echo "cutover: note — ${NOTES[$i]}" >&2
done

if (( ${#REFUSE[@]} )); then
  echo "error: the cutover cannot place these entries, and it will not guess:" >&2
  for e in "${REFUSE[@]}"; do printf '\n  %s\n' "$e" >&2; done
  printf '\n' >&2
  echo "Everything this script retires must land in the sink as content, or be provably worthless — a pointer with nothing behind it. Anything else is refused HERE, before the first byte is written, because a refusal raised halfway through the sync leaves the sink dirty and the clean-sink gate then blocks every later run. Resolve the entries above, then re-run. Nothing was changed." >&2
  exit 1
fi

if (( ${#DIV_REL[@]} )); then
  echo "error: these files exist in BOTH trees with different bytes, and this script does not get to choose between them:" >&2
  for i in "${!DIV_REL[@]}"; do
    printf '\n  %s\n' "${DIV_REL[$i]}" >&2
    diff -u -- "${DIV_DST[$i]}" "${DIV_SRC[$i]}" >&2 || true
  done
  echo "" >&2
  echo "Reconcile them by hand (the sink copy is the '---' side, the ~/.claude copy the '+++' side), commit the result in $SINK, then re-run. Nothing was changed." >&2
  exit 1
fi

# ------------------------------------------------------------- the sync -----
# Preflight cannot rule out ENOSPC, EIO, or a writer removing a live file
# between the plan and the copy, and any of them used to abort under `set -e`
# with a raw coreutils message and a half-populated sink — which the clean-sink
# gate then turned into a permanent block on every later run, the one outcome
# this script's own comments call worse than doing nothing. So the sync unwinds
# itself: it removes exactly the paths it created and restores exactly the
# tracked file it rewrote, leaving the sink as it found it.
CREATED_FILES=(); CREATED_DIRS=(); RESTORE_FILES=(); SYNC_ACTIVE=0

rollback_sync() {
  local i
  # Unstage first. If `git add -A` succeeded and the commit did not, the index
  # still lists everything, and removing the files from the worktree would
  # leave the sink dirty as `AD` — the very block being unwound. A MIXED reset
  # (never --hard): it touches the index only, and the clean-sink gate already
  # proved nothing else was staged.
  git -C "$SINK" reset -q 2>/dev/null || true
  for ((i=${#CREATED_FILES[@]}-1; i>=0; i--)); do
    rm -f -- "${CREATED_FILES[$i]}" 2>/dev/null || true
  done
  for ((i=${#CREATED_DIRS[@]}-1; i>=0; i--)); do
    rmdir -- "${CREATED_DIRS[$i]}" 2>/dev/null || true
  done
  for ((i=${#RESTORE_FILES[@]}-1; i>=0; i--)); do
    git -C "$SINK" checkout -q HEAD -- "${RESTORE_FILES[$i]}" 2>/dev/null || true
  done
  if [[ -n "$(git -C "$SINK" status --porcelain 2>/dev/null)" ]]; then
    echo "cutover: WARNING — the sink $SINK is still dirty after unwinding; the next run will refuse until you clean it up:" >&2
    git -C "$SINK" status --porcelain >&2 || true
  fi
}

on_exit() {
  local rc=$?
  if [[ "$SYNC_ACTIVE" == 1 ]] && (( rc != 0 )); then
    echo "cutover: unwinding this run's partial writes to the sink, so the clean-sink gate does not block the next run" >&2
    rollback_sync
  fi
}
trap on_exit EXIT

ensure_dir() {  # mkdir -p, but remembering which levels WE created
  local d="$1"
  [[ -d "$d" ]] && return 0
  ensure_dir "$(dirname -- "$d")" || return 1
  mkdir -- "$d" || return 1
  CREATED_DIRS+=("$d")
}

SYNC_ACTIVE=1
for ((i=0; i<${#COPY_SRC[@]}; i++)); do
  echo "cutover: copied into the sink: ${COPY_DST[$i]#"$SINK"/}"
  ensure_dir "$(dirname -- "${COPY_DST[$i]}")" \
    || die "could not create $(dirname -- "${COPY_DST[$i]}") in the sink"
  # Recorded BEFORE the copy: a cp that fails half-written still leaves a file
  # behind, and the unwind has to know about it.
  CREATED_FILES+=("${COPY_DST[$i]}")
  # -L: a symlinked proposal lands in the sink as its content. A copy of the
  # pointer would break the day the backup directory is deleted.
  cp -p -L -- "${COPY_SRC[$i]}" "${COPY_DST[$i]}" \
    || die "could not copy ${COPY_REL[$i]} into the sink"
done
install_gate_config

git -C "$SINK" add -A
if git -C "$SINK" diff --cached --quiet; then
  echo "cutover: sink already held every byte; nothing to commit"
else
  git -C "$SINK" commit -q -m "cutover: absorb proposal content and repoint gate-config at the sink"
  echo "cutover: committed the absorbed content in $SINK"
fi
SYNC_ACTIVE=0

# ------------------------------------------------------------- verify --------
# Hard gate. After the sync every live file must exist in the sink and be
# byte-identical; anything else means the sync could not do its job (a failed
# copy, an unreadable file, a sink that would not commit) and we must not
# remove the live tree. `-L` is tested first because `-f` and `cmp` FOLLOW a
# symlink: a sink entry pointing back at the live file compared the live file
# with itself and sailed through.

verify_failed=0
for ((i=0; i<${#VER_SRC[@]}; i++)); do
  if [[ -L "${VER_DST[$i]}" ]]; then
    echo "  the sink side is a symlink, not content: ${VER_REL[$i]}" >&2; verify_failed=1
  elif [[ ! -f "${VER_DST[$i]}" ]]; then
    echo "  missing in the sink: ${VER_REL[$i]}" >&2; verify_failed=1
  elif ! cmp -s -- "${VER_SRC[$i]}" "${VER_DST[$i]}"; then
    echo "  differs from the sink: ${VER_REL[$i]}" >&2; verify_failed=1
  fi
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
BACKUP=""

# `mv` into an EXISTING stamp directory nests rather than clobbers, so the
# retired tree landed one level deeper than the closing message claims — at
# BACKUP/<stamp>/claude-proposals/proposals — which is exactly the sort of
# quiet mismatch that gets the wrong thing deleted later. A stamp collision now
# takes a directory of its own instead.
new_backup_dir() {
  [[ -n "$BACKUP" ]] && return 0
  mkdir -p -- "$BACKUP_ROOT" || return 1
  if [[ ! -e "$BACKUP_ROOT/$STAMP" ]] && mkdir -- "$BACKUP_ROOT/$STAMP" 2>/dev/null; then
    BACKUP="$BACKUP_ROOT/$STAMP"
  else
    BACKUP="$(mktemp -d -- "$BACKUP_ROOT/$STAMP.XXXXXX")" || return 1
  fi
}

RETIRED_SRC=(); RETIRED_DST=()

for i in "${!LINKS[@]}"; do
  pair="${LINKS[$i]}"; link="${pair%%:*}"; want="${pair#*:}"

  case "${TARGET_STATE[$i]}" in
    linked)
      echo "cutover: $link already points at $want — left alone"
      continue
      ;;
    drop)
      current="$(readlink -- "$link")"
      rm -- "$link"   # a pointer with nothing behind it: removing it loses nothing
      echo "cutover: retargeted $link (was -> $current)"
      ;;
    dir)
      new_backup_dir || die "could not create a backup directory under $BACKUP_ROOT"
      parent="$(basename -- "$(dirname -- "$link")")"
      dest="$BACKUP/${parent#.}-$(basename -- "$link")"   # no leading dot: the backup should not be hidden
      mv -- "$link" "$dest"
      echo "cutover: moved $link -> $dest (preserved, not deleted)"
      RETIRED_SRC+=("$dest"); RETIRED_DST+=("$want")
      ;;
    absent) ;;
  esac

  ln -s -- "$want" "$link"
  echo "cutover: $link -> $want"
done

# ------------------------------------------- the verify -> mv window ---------
# There is no lock the proposal writers take, and there cannot usefully be one:
# they are unattended cron agents, and a lock only one side honours is theatre.
# So a proposal can land in the live tree after verify has passed and before
# the `mv` — it went to the backup, the run said "done", and the only copy sat
# in a directory the closing message invites you to delete.
#
# The fix does not need the writers' cooperation. After the swap the retired
# tree is at a path no writer knows, so it has stopped moving: whatever is in
# there that pass 1 never saw arrived during the run, and it is absorbed and
# committed now. Nothing here can refuse — the swap has happened — so anything
# unplaceable is reported loudly and left where it is preserved.
absorbed=0
for ((i=0; i<${#RETIRED_SRC[@]}; i++)); do
  live="${RETIRED_SRC[$i]}"; dest="${RETIRED_DST[$i]}"
  while IFS= read -r -d '' src; do
    skip_path "$src" && continue
    rel="${src#"$live"/}"
    dst="$dest/$rel"
    [[ -n "${PLANNED[$dst]:-}" ]] && continue    # pass 1 already judged this node
    if [[ -d "$src" && ! -L "$src" ]]; then continue; fi
    if [[ -L "$src" || ! -f "$src" || ! -r "$src" ]]; then
      echo "cutover: WARNING — $src appeared in the live tree during this run and is not a plain readable file, so it was NOT absorbed into the sink. It is preserved where it is." >&2
      continue
    fi
    if [[ -e "$dst" || -L "$dst" ]]; then continue; fi
    if ! ensure_dir "$(dirname -- "$dst")" || ! cp -p -- "$src" "$dst"; then
      echo "cutover: WARNING — $src appeared during this run and could not be absorbed into the sink. It is preserved where it is." >&2
      continue
    fi
    echo "cutover: absorbed ${dst#"$SINK"/} — written during the run, after the verify pass"
    absorbed=1
  done < <(find "$live" -mindepth 1 -print0)
done
if (( absorbed )); then
  git -C "$SINK" add -A
  if ! git -C "$SINK" diff --cached --quiet; then
    git -C "$SINK" commit -q -m "cutover: absorb proposals written during the swap"
    echo "cutover: committed the proposals that landed during the run"
  fi
fi

echo "cutover: done. Spine: $SINK"
if [[ -n "$BACKUP" && -d "$BACKUP" ]]; then
  echo "cutover: the replaced directories are preserved at $BACKUP — delete them yourself once you are satisfied."
fi
