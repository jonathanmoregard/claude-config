#!/usr/bin/env bash
# SessionStart hook: report when the ~/.claude MAIN checkout is off master or
# has a dirty tree, and print the recovery commands.
#
# It observes and reports, and it installs the ACTIVATION for the enforcing
# git-layer hook (core.hooksPath) — it does not itself enforce anything. The
# enforcing files are `.githooks/reference-transaction` and
# `.githooks/prepare-commit-msg`, once core.hooksPath points at them. This
# script must never block a session, so `set -e` is deliberately omitted,
# every git call is guarded, and it always exits 0. Silent when the checkout
# is on master, clean, and the git-layer pin is already active.

set -uo pipefail

MAIN="${CLAUDE_PRIMARY_CHECKOUT:-$HOME/.claude}"

git -C "$MAIN" rev-parse --show-toplevel >/dev/null 2>&1 || exit 0

# Only the MAIN worktree is pinned. In a linked worktree `--git-dir` is
# <common>/worktrees/<name>; in the main worktree the two are the same dir.
# Same structural test the Git-layer guard uses — no hardcoded worktree path.
git_dir="$(git -C "$MAIN" rev-parse --path-format=absolute --git-dir 2>/dev/null || true)"
common_dir="$(git -C "$MAIN" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
if [ -z "$git_dir" ] || [ "$git_dir" != "$common_dir" ]; then
  exit 0
fi

# Drift is report-only. The installer can edit repository config, executable
# bits, .git/info/exclude, and missing forwarders, so do not invoke it while
# the primary checkout is off master or has TRACKED changes (unknown work).
# Untracked strays are reported but do not hold activation back: the installer
# never touches them, and a pin left off because of a stray file is the worse
# failure (the live checkout has carried MEMORY.md and `e` for weeks).
branch="$(git -C "$MAIN" symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
dirty="$(git -C "$MAIN" status --porcelain=v1 --untracked-files=all 2>/dev/null || true)"
tracked_dirty="$(git -C "$MAIN" status --porcelain=v1 --untracked-files=no 2>/dev/null || true)"
untracked="$(git -C "$MAIN" ls-files --others --exclude-standard 2>/dev/null || true)"
# `/model` writes settings.json's "model" key into the live checkout, and
# claude-pull.sh carries exactly that difference across pulls. A settings.json
# whose ONLY difference from HEAD is "model" is expected state, not unknown work.
if [ "$tracked_dirty" = " M settings.json" ] && command -v python3 >/dev/null 2>&1 \
  && git -C "$MAIN" show HEAD:settings.json 2>/dev/null | python3 -c '
import json, sys
try:
    head = json.load(sys.stdin)
    local = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError):
    raise SystemExit(1)
if not (isinstance(head, dict) and isinstance(local, dict)):
    raise SystemExit(1)
strip = lambda d: {k: v for k, v in d.items() if k != "model"}
raise SystemExit(0 if strip(head) == strip(local) else 1)
' "$MAIN/settings.json" 2>/dev/null; then
  tracked_dirty=""
fi
main_real="$(readlink -f "$MAIN" 2>/dev/null || printf '%s' "$MAIN")"
linked_main_paths=()
_wt=""
while IFS= read -r _line; do
  case "$_line" in
    "worktree "*) _wt="${_line#worktree }" ;;
    "branch refs/heads/master")
      _wt_real="$(readlink -f "$_wt" 2>/dev/null || printf '%s' "$_wt")"
      if [ -n "$_wt" ] && [ "$_wt_real" != "$main_real" ]; then
        linked_main_paths+=("$_wt")
      fi
      ;;
  esac
done < <(git -C "$MAIN" worktree list --porcelain 2>/dev/null || true)

primary_drift=0
if [ "$branch" != master ] || [ -n "$tracked_dirty" ]; then
  primary_drift=1
fi

if [ "$primary_drift" -eq 0 ] && [ -n "$untracked" ]; then
  printf '[claude-policy] note: %s untracked path(s) in %s (live checkout, not a workspace): %s\n' \
    "$(printf '%s\n' "$untracked" | wc -l)" "$MAIN" \
    "$(printf '%s\n' "$untracked" | head -5 | paste -sd' ' -)"
  printf '  Not blocking: the pin stays active and no work is held up.\n\n'
fi

if [ "$primary_drift" -eq 1 ] || [ "${#linked_main_paths[@]}" -gt 0 ]; then
  if [ "$primary_drift" -eq 1 ]; then
    printf '[claude-policy] %s is the primary integration and auto-pull checkout. It is\n' "$MAIN"
    printf 'expected to sit on master with a clean tree; right now it does not.\n\n'
  else
    printf '[claude-policy] %s (primary checkout) is clean on master, but another\n' "$MAIN"
    printf 'worktree is attached to local master, so scheduled pulls move that worktree'"'"'s HEAD\n'
    printf 'underneath its files.\n\n'
  fi

  if [ "$branch" != master ]; then
    printf '  off master — HEAD is on: %s\n' "${branch:-<detached>}"
  fi
  if [ -n "$dirty" ]; then
    printf '  dirty tree — %s uncommitted path(s):\n' "$(printf '%s\n' "$dirty" | wc -l)"
    printf '%s\n' "$dirty" | head -10 | sed 's/^/      /'
  fi
  if [ "${#linked_main_paths[@]}" -gt 0 ]; then
    printf '  linked worktree also attached to local master (%s):\n' "${#linked_main_paths[@]}"
    for _wt in "${linked_main_paths[@]}"; do
      printf '      %s\n' "$_wt"
      # The worktree's own HEAD reflog is not written when the primary
      # fast-forwards master, so its newest entry is the commit the worktree's
      # files were last checked out at. Detaching there is lossless: git
      # refuses a switch that would overwrite local changes, and genuine edits
      # stay as ordinary modifications against that commit.
      _at="$(git -C "$_wt" reflog -n1 --format=%H HEAD 2>/dev/null || true)"
      if [ -n "$_at" ] && git -C "$_wt" cat-file -e "$_at^{commit}" 2>/dev/null; then
        _edits="$(git -C "$_wt" diff --name-only "$_at" 2>/dev/null | wc -l)"
        _untracked="$(git -C "$_wt" ls-files --others --exclude-standard 2>/dev/null | wc -l)"
        printf '        last checkout at %s; since then %s tracked path(s) edited, %s untracked\n' \
          "$(git -C "$_wt" rev-parse --short "$_at" 2>/dev/null)" "$_edits" "$_untracked"
        if [ -z "$(git -C "$_wt" diff --cached --name-only "$_at" 2>/dev/null)" ]; then
          printf '        repair: git -C %q switch --detach %s\n' "$_wt" "$_at"
        else
          printf '        staged changes differ from that commit — no one-line repair; inspect by hand\n'
        fi
      else
        printf '        no HEAD reflog — cannot tell which commit its files reflect\n'
      fi
    done
  fi

  if [ "$primary_drift" -eq 1 ]; then
    cat <<'EOF'

Agent action: stop and report exact state to Jonathan. Do not stash, reset,
clean, switch branches, or move unknown work. This concerns ~/.claude only;
work in other repositories proceeds as normal. Start new ~/.claude development
only after the primary checkout is clean on master, using:
EOF
  else
    cat <<'EOF'

Agent action: report the worktree(s) above to Jonathan once and leave them
untouched — they may hold another session's work. The "repair" line is his to
run: it detaches that worktree at its last checkout, keeps every
edit, and frees local master. New development is unaffected; start it as usual:
EOF
  fi
  cat <<'EOF'

  ~/.claude/scripts/claude-worktree.sh <slug>
  cd ~/worktrees/claude-<slug>

.githooks/ refuses commits and branch-leaving switch/checkout in the primary
checkout, plus any linked-worktree attachment or update to local master. Read-only
git and `git worktree add` are untouched. Local master may move only by a primary
fast-forward so scheduled pulls keep working; rebases use origin/master.
EOF
  # Unknown work in the primary: never run the installer over it. A linked
  # worktree on master alone does not hold activation back.
  [ "$primary_drift" -eq 1 ] && exit 0
  printf '\n'
fi

# Self-heal the git-layer floor. core.hooksPath is local config, so it does not
# travel with a clone or survive a fresh checkout — an enforcement layer that
# waits for someone to remember a setup command is not enforcing. Installing it
# is idempotent and is the "trigger" half of what hooks are for; the decision
# it enforces still lives in .githooks/, not here.
#
# Two things independently gate whether the git layer actually runs, and
# either one being wrong must reach a WARNING — never silence:
#   1. core.hooksPath must be an ABSOLUTE path resolving to this repo's
#      tracked .githooks dir. It must be absolute, not merely resolve to the
#      right place: a relative `.githooks` is bypassable via an explicit
#      GIT_DIR from an unrelated cwd, and silently runs no hooks at all in a
#      linked worktree whose branch predates .githooks (see
#      scripts/install-worktree-policy.sh's own header for both, reproduced on git
#      2.55). A stale relative value from before that installer was hardened
#      must therefore be treated as NOT ok, so it gets upgraded rather than
#      accepted as already-healthy.
#   2. the hook file itself must still be executable. A clone or a plain file
#      copy can drop that bit; git then skips the hook silently — no error,
#      no advice hint the harness surfaces — and nothing before this fix ever
#      re-checked it once core.hooksPath already looked right.
expected_hooks_dir="$MAIN/.githooks"
raw_hooks_path="$(git -C "$MAIN" config --local --get core.hooksPath 2>/dev/null || true)"
rp_expected="$(readlink -f "$expected_hooks_dir" 2>/dev/null || true)"
hookspath_ok=0
case "$raw_hooks_path" in
  /*)
    rp_resolved="$(readlink -f "$raw_hooks_path" 2>/dev/null || true)"
    [ -n "$rp_resolved" ] && [ "$rp_resolved" = "$rp_expected" ] && hookspath_ok=1
    ;;
esac

hook_exec_ok=0
[ -x "$expected_hooks_dir/reference-transaction" ] && hook_exec_ok=1

#   3. every global hook must still have a resolvable forwarder here. This is
#      the steady-state hole: health used to be judged by hooksPath plus the
#      reference-transaction exec bit alone, and both of those go on being true
#      forever once set. So the FIRST successful run permanently silenced the
#      installer, and anything that went wrong afterwards was shadowed with no
#      message — a global hook added later (home-manager gains a pre-push, say)
#      never got a forwarder, and a forwarder symlink whose target went missing
#      stayed dangling. Either one silently removes a hook from this repo, in
#      every worktree; the gitleaks pre-commit is one of them. Checking it here
#      is cheap and turns "install once" into "keep it true".
forwarders_ok=1
global_hooks_dir="$(git config --global --get core.hooksPath 2>/dev/null || true)"
case "$global_hooks_dir" in
  "~/"*) global_hooks_dir="$HOME/${global_hooks_dir#\~/}" ;;
  "~")   global_hooks_dir="$HOME" ;;
esac
# A forwarder that no longer resolves. -L true with -e false is exactly that.
for _h in "$expected_hooks_dir"/*; do
  if [ -L "$_h" ] && [ ! -e "$_h" ]; then forwarders_ok=0; break; fi
done
# A global hook with no forwarder of the same name.
if [ "$forwarders_ok" -eq 1 ] && [ -n "$global_hooks_dir" ] && [ -d "$global_hooks_dir" ]; then
  for _g in "$global_hooks_dir"/*; do
    [ -f "$_g" ] && [ -x "$_g" ] || continue
    _n="${_g##*/}"
    case "$_n" in _*) continue ;; esac
    if [ ! -e "$expected_hooks_dir/$_n" ]; then forwarders_ok=0; break; fi
  done
fi

installer="$MAIN/scripts/install-worktree-policy.sh"
installer_ok=0
[ -x "$installer" ] && installer_ok=1

if [ "$hookspath_ok" -eq 1 ] && [ "$hook_exec_ok" -eq 1 ] && [ "$forwarders_ok" -eq 1 ]; then
  : # pin active, hook runnable, no shadowed global hook — nothing to do
elif [ "$installer_ok" -eq 1 ]; then
  # Captured, not discarded (finding 3): the installer's own warnings — e.g.
  # a global hook it would silently shadow — are exactly what a human needs
  # to see here, on both success and failure.
  install_output="$("$installer" "$MAIN" 2>&1)"
  install_rc=$?
  if [ "$install_rc" -eq 0 ]; then
    if [ "$hookspath_ok" -eq 1 ] && [ "$hook_exec_ok" -eq 1 ]; then
      # The pin itself was already fine; what needed fixing was a global hook
      # left without a forwarder, or a forwarder that stopped resolving.
      printf '[claude-policy] repaired the git-layer hook forwarders (a global hook was\n'
      printf '  being shadowed for this repo, or a forwarder symlink had broken).\n'
    else
      printf '[claude-policy] activated the git-layer branch pin (core.hooksPath -> %s).\n' "$expected_hooks_dir"
    fi
  else
    printf '[claude-policy] WARNING: could not activate the git-layer branch pin.\n'
    printf '  Run: %s %s\n' "$installer" "$MAIN"
    printf '  Until it is set, agent instructions are the only active layer.\n'
  fi
  if [ -n "$install_output" ]; then
    printf '%s\n' "$install_output" | sed 's/^/  [install-worktree-policy] /'
  fi
  printf '\n'
else
  # hooksPath wrong/missing, or the hook file is not executable, AND the
  # installer itself is missing or has lost its exec bit — this branch must
  # be reached rather than skipped, or the pin silently stays absent.
  printf '[claude-policy] WARNING: cannot activate the git-layer branch pin — installer\n'
  printf '  missing or not executable: %s\n' "$installer"
  printf '  Until it is set, agent instructions are the only active layer.\n\n'
fi

exit 0
