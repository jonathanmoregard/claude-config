#!/usr/bin/env bash
# Activate the git-level enforcement floor on a ~/.claude checkout by pointing
# core.hooksPath at the MAIN worktree's .githooks directory, by absolute path.
#
#   scripts/install-worktree-policy.sh <path-to-dot-claude-checkout>
#
# Idempotent and safe to re-run: it sets one config key, verifies the result,
# and reports. It refuses any path that is not a clone of the ~/.claude repository.
# Passing a linked worktree is fine — the main worktree is resolved from it.
#
# What activation buys, and what it costs:
#   * .githooks/reference-transaction enforces primary ownership of local master
#     and pins the MAIN worktree (see that file's header for the exact rule).
#   * .githooks/prepare-commit-msg refuses every commit in the primary
#     checkout (and on local master anywhere); .githooks/pre-push refuses
#     pushes from the primary checkout.
#   * core.hooksPath overrides the machine-wide hooks directory for this repo,
#     in every worktree and for every hook type. .githooks/pre-commit is a
#     wrapper around .githooks/_forward-global-hook, and pre-push,
#     prepare-commit-msg and reference-transaction call the global hook of the
#     same name first; each preserves the global hook's exit status. If a NEW global hook appears later with no delegate here, it is
#     silently dropped for this repo — this script warns when it spots one.

set -uo pipefail

die()  { printf 'install-worktree-policy: %s\n' "$*" >&2; exit 1; }
note() { printf '  %s\n' "$*"; }

[ $# -eq 1 ] || die "usage: $(basename "$0") <path-to-dot-claude-checkout>"

target="$1"
[ -d "$target" ] || die "not a directory: $target"

# Resolve through the caller's path to a real git working tree.
top="$(git -C "$target" rev-parse --show-toplevel 2>/dev/null)" \
  || die "not a git working tree: $target"

# --- refuse anything that is not the ~/.claude repository --------------------
# Helper and puller fetch the remote named `origin`, so validate that exact
# remote rather than accepting a matching secondary remote. Scope by remote,
# not filesystem path: relocated checkouts still work; similarly named repos
# do not.
origin_url="$(git -C "$top" config --local --get remote.origin.url 2>/dev/null)"
if ! [[ "$origin_url" =~ [:/]jonathanmoregard/(\.claude|claude-config)(\.git)?/?$ ]]; then
  die "refusing: $top origin does not point at jonathanmoregard/claude-config.
  These hooks pin a branch and refuse ref updates; installing them on the
  wrong repository would be far worse than not installing them at all."
fi

# --- always anchor on the MAIN worktree -------------------------------------
# core.hooksPath lives in the SHARED config, so whatever is set here is what
# every linked worktree uses too. Pointing it at a linked worktree's tree would
# make the whole repo's hooks depend on that worktree's branch, and vanish when
# it is removed. `git worktree list` documents the main worktree as the first
# entry, and reports the same order from inside any worktree.
main_top="$(git -C "$top" worktree list --porcelain 2>/dev/null \
            | sed -n '1s/^worktree //p')"
[ -n "$main_top" ] && [ -d "$main_top" ] \
  || die "cannot locate the main worktree of $top"
if [ "$main_top" != "$top" ]; then
  note "given a linked worktree; anchoring on the main worktree: $main_top"
fi

# --- where machine-local state goes -----------------------------------------
# The shared git dir, so this works identically from any worktree.
common_dir="$(git -C "$main_top" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)"
hooks_dir="$main_top/.githooks"

# Do not silently replace repository-specific hooks. Only the machine-global
# hook directory can be forwarded generically; an unrelated repository-local
# hooksPath may contain project logic that must be migrated deliberately.
local_hooks_path="$(git -C "$main_top" config --local --get core.hooksPath 2>/dev/null)"
case "$local_hooks_path" in
  ""|"$hooks_dir"|.githooks) ;;
  *)
    die "refusing to replace existing repository-local core.hooksPath: $local_hooks_path
  Migrate those hooks into $hooks_dir explicitly, then re-run this installer."
    ;;
esac

# Keep an AUTO-CREATED forwarder out of git's way, by listing it in
# <common>/info/exclude.
#
# WHY THIS IS NEEDED. A forwarder this script creates lands UNTRACKED inside
# the pinned main checkout. It cannot be committed from there — both
# enforcement layers refuse — so the durable fix is to commit it from a
# worktree and merge, which is exactly what the note at the end of this script
# requires. The pull that then brings the same path in as a TRACKED file
# aborts with "untracked working tree files would be overwritten by merge",
# wedging scripts/claude-pull.sh: it alerts, exits 1, and keeps doing so every
# tick until a human clears the file by hand. The protected checkout stops
# tracking origin — caused by the repair path, on the machine it was meant to repair.
#
# MEASURED on git 2.55: a fast-forward merge aborts on an untracked path, and
# does NOT abort when that path is listed in .git/info/exclude — it replaces
# the ignored file with the tracked one and leaves a clean tree. It does so
# even when the ignored file is a regular file and the incoming one a symlink.
#
# info/exclude is the right home for it on both counts: it is machine-local
# state about a machine-local activation, and it lives in the git dir rather
# than the work tree, so it can never itself become the untracked file that
# blocks a pull. Only UNTRACKED entries are ever listed — excluding a tracked
# path is a no-op today, but would silently hide the file if it were removed
# upstream later.
exclude_local() {
  local name="$1" line="/.githooks/$1" f
  [ -n "$common_dir" ] || return 0
  f="$common_dir/info/exclude"
  mkdir -p "$common_dir/info" 2>/dev/null || return 0
  if [ -f "$f" ] && grep -qxF "$line" "$f" 2>/dev/null; then return 0; fi
  printf '%s\n' "$line" >> "$f" 2>/dev/null || \
    note "WARNING: could not add $line to $f — a later pull that brings $name in as a tracked file will abort"
}

# --- the hooks themselves must be present and runnable ----------------------
[ -d "$hooks_dir" ] || die \
  "no .githooks directory in the main worktree $main_top — is it on a branch that predates it?"

primary="$hooks_dir/reference-transaction"
[ -f "$primary" ] || die "missing $primary"

forwarder="_forward-global-hook"
[ -f "$hooks_dir/$forwarder" ] || die "missing $hooks_dir/$forwarder"

# --- repair dangling forwarder symlinks -------------------------------------
# Installer-created .githooks/<hook-name> entries are symlinks to
# _forward-global-hook; tracked entries may instead be small wrappers. A
# DANGLING symlink used to sail straight through installation: `-f` skipped it in the
# chmod/syntax loop below, `-L` suppressed the shadow warning further down, and
# verification only ever looked at reference-transaction. Net effect — the
# installer printed "active" while pre-commit forwarded nothing and the
# machine-wide gitleaks scan was silently off for this repo.
#
# `-L` true with `-e` false is exactly "a symlink whose target does not
# resolve"; every other -e/-L combination is a healthy file.
for h in "$hooks_dir"/*; do
  [ -L "$h" ] && [ ! -e "$h" ] || continue
  name="${h##*/}"
  note "repairing dangling forwarder: $name -> $forwarder"
  ln -sfn "$forwarder" "$h" || die \
    "cannot repair the dangling forwarder $h
  Until it resolves, git runs NO $name hook for this repo at all:
  core.hooksPath shadows the machine-wide one and this link forwards nothing."
  [ -e "$h" ] || die "repaired $h but it still does not resolve"
done

for h in "$hooks_dir"/*; do
  # -f/-x/bash -n all follow symlinks, which is what the forwarder links are.
  [ -f "$h" ] || continue
  if [ ! -x "$h" ]; then
    chmod +x "$h" || die "cannot make executable: $h"
    note "made executable: ${h#"$main_top"/}"
  fi
  bash -n "$h" 2>/dev/null || die "syntax error in $h — refusing to install"
done

# --- keep every global hook running -----------------------------------------
# Setting core.hooksPath replaces the global hooks directory wholesale for this
# repo. Any global hook without a same-named forwarder in .githooks stops
# running here, silently.
#
# This used to print an `ln -s` suggestion and exit 0. That left enforcement
# resting on a human noticing a one-time note and acting on it — and the whole
# reason this layer exists is that enforcement which waits to be remembered is
# not enforcement. The repair is a single symlink, mechanically identical to
# the ones already committed to .githooks, so make it instead of suggesting it.
global_dir="$(git config --global --get core.hooksPath 2>/dev/null)"
case "$global_dir" in
  "~/"*) global_dir="$HOME/${global_dir#\~/}" ;;
  "~")   global_dir="$HOME" ;;
esac
created=()
nonexec=()
if [ -n "$global_dir" ] && [ -d "$global_dir" ]; then
  for g in "$global_dir"/*; do
    [ -f "$g" ] || continue
    name="${g##*/}"
    # git only runs files named after a hook, so a leading underscore is inert
    # (that is why the forwarder itself is named that way) — nothing to mirror.
    case "$name" in _*) continue ;; esac
    if [ ! -x "$g" ]; then
      # git skips a non-executable hook; so would a forwarder pointed at it.
      # Collected here rather than silently ignored — see the warning below.
      nonexec+=("$name")
      continue
    fi
    [ -e "$hooks_dir/$name" ] && continue
    # Exclude BEFORE creating, so the path is never briefly visible to a
    # concurrent pull as a plain untracked file.
    exclude_local "$name"
    ln -sfn "$forwarder" "$hooks_dir/$name" || die \
      "cannot create the forwarder $hooks_dir/$name
  Without it, the global $name hook does not run for this repo in any worktree."
    [ -e "$hooks_dir/$name" ] || die "created $hooks_dir/$name but it does not resolve"
    created+=("$name")
  done
fi

# Re-assert the exclusions on every run, not only on the run that created the
# link. info/exclude is machine-local and can be wiped, restored from a backup,
# or predate this script entirely, while .githooks/<name> goes on existing —
# and the wedge is back the moment the two disagree. This also covers a
# forwarder created by an older version of this script.
#
# `ls-files --error-unmatch` answers the tracked question with an exit status,
# and only TWO of its statuses are an answer: 0 tracked, 1 untracked. Anything
# else (128 on a locked or missing index or a broken repo, 127 if git is not
# on PATH, 124 on a timeout) means git never answered — and reading "not 0" as
# "untracked", which this loop used to do, spends one transient failure on a
# PERMANENT wrong entry: a tracked path in info/exclude is silent today and
# hides the file the day it is removed upstream, which is precisely what the
# WHY block above says must never happen. So discriminate, and fail closed on
# the rest: skip the exclusion, say so loudly, and let the next run redo it
# (the SessionStart hook re-runs this script) once git can answer again.
for h in "$hooks_dir"/*; do
  name="${h##*/}"
  [ "$name" = "*" ] && continue
  git -C "$main_top" ls-files --error-unmatch -- ".githooks/$name" >/dev/null 2>&1
  rc=$?
  case "$rc" in
    0) continue ;;              # tracked — must never be excluded
    1) exclude_local "$name" ;; # untracked — the case this loop exists for
    *)
      printf '  WARNING: `git ls-files --error-unmatch .githooks/%s` exited %s\n' \
        "$name" "$rc" >&2
      printf '  (only 0/1 are answers), so git could not say whether it is tracked.\n' >&2
      printf '  Not excluding it — a tracked path in info/exclude is permanent and\n' >&2
      printf '  silent. Re-run this script once the repository responds again.\n' >&2
      ;;
  esac
done

# --- set it -----------------------------------------------------------------
# An ABSOLUTE path, and it must stay absolute. A relative `.githooks` is
# resolved by git against whatever it decides the working-tree top is, and both
# ways that can go are broken — both reproduced on git 2.55:
#
#   * Under an explicit GIT_DIR from an unrelated cwd
#     (`cd /tmp && GIT_DIR=<repo>/.git git update-ref refs/heads/x HEAD`) the
#     top is the cwd, `.githooks` is not there, and NO hook runs. The pin was
#     bypassable by a one-token env prefix.
#   * In a linked worktree whose branch predates .githooks, the directory does
#     not exist in that tree — so git runs no hooks AT ALL, including the
#     machine-wide gitleaks pre-commit that core.hooksPath is shadowing. Every
#     existing worktree of this repo is on such a branch, so a relative path
#     would silently switch off secret scanning in all of them. Measured:
#     hooksPath=.githooks with the directory absent => nothing ran;
#     hooksPath unset => the global pre-commit ran.
#
# The cost of absolute is that a moved checkout points at a path that no longer
# exists, which fails open. That is bounded: the SessionStart hook re-runs this
# script, and the value below is recomputed from the current location every
# time, so the first session after a move repairs it.
want="$hooks_dir"
current="$(git -C "$main_top" config --get core.hooksPath 2>/dev/null)"
if [ "$current" = "$want" ]; then
  note "core.hooksPath already set to $want — nothing to change"
else
  [ -n "$current" ] && note "replacing existing core.hooksPath: $current"
  git -C "$main_top" config core.hooksPath "$want" \
    || die "failed to set core.hooksPath in $main_top"
  note "set core.hooksPath = $want"
fi

# --- verify -----------------------------------------------------------------
verified="$(git -C "$main_top" config --get core.hooksPath 2>/dev/null)"
[ "$verified" = "$want" ] \
  || die "verification failed: core.hooksPath is '$verified', expected '$want'"
case "$verified" in
  /*) ;;
  *)  die "verification failed: core.hooksPath '$verified' is not absolute" ;;
esac
[ -x "$primary" ] || die "verification failed: $primary is not executable"

# Verify EVERY hook, not just the pin. Checking only reference-transaction is
# what let a dangling pre-commit forwarder be reported as a successful install.
for h in "$hooks_dir"/*; do
  name="${h##*/}"
  [ -e "$h" ] || die "verification failed: $hooks_dir/$name does not resolve"
  [ -x "$h" ] || die "verification failed: $hooks_dir/$name is not executable"
done

git_dir="$(git -C "$top" rev-parse --path-format=absolute --git-dir 2>/dev/null)"
common_dir="$(git -C "$top" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)"
if [ "$git_dir" = "$common_dir" ]; then
  where="main worktree (the pin is ACTIVE here)"
else
  where="linked worktree (the pin never fires here; it is now active in the main worktree too, since core.hooksPath is shared)"
fi

printf '\ninstall-worktree-policy: active on %s\n' "$main_top"
note "context:       $where"
note "hooksPath:     $want"
note "hooks:         $(cd "$hooks_dir" && printf '%s ' *)"
note "rule:          local master moves only by primary fast-forward; linked worktrees use feature branches and rebase onto origin/master"

if [ "${#created[@]}" -gt 0 ]; then
  printf '\n  Created forwarders for global hooks in %s that had none —\n' "$global_dir"
  printf '  without these they would not run for this repo, in any worktree:\n'
  for s in "${created[@]}"; do printf '    - %s -> %s\n' "$s" "$forwarder"; done
  printf '  They are untracked, and listed in %s/info/exclude so they\n' "$common_dir"
  printf '  neither dirty this checkout nor block the pull that later brings the same\n'
  printf '  paths in as tracked files. Committing them from a worktree is still the\n'
  printf '  durable fix (a fresh clone gets them) and will now land cleanly.\n'
fi

if [ "${#nonexec[@]}" -gt 0 ]; then
  printf '\n  WARNING — these files in %s are hooks by name but are NOT\n' "$global_dir" >&2
  printf '  executable, so git skips them and no forwarder was made for them.\n' >&2
  printf '  If one of them is the gitleaks pre-commit, secret scanning is OFF:\n' >&2
  for s in "${nonexec[@]}"; do printf '    - %s/%s\n' "$global_dir" "$s" >&2; done
  printf '  Restore it with:  chmod +x %s/<name>\n' "$global_dir" >&2
fi

printf '\nTo deactivate:  git -C %s config --unset core.hooksPath\n' "$main_top"
