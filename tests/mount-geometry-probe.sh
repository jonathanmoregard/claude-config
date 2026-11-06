#!/usr/bin/env bash
# Does the projected layout actually resolve once it is mounted?
#
# The layout rests on one geometric claim: `runtime/` sits exactly one level
# below the repository root, and the mounted view reproduces that, so a skill
# symlink spelled `../../vendor/y` reaches `<repo>/vendor/y` on disk and
# `~/.claude/vendor/y` under the mounts. If that is wrong, 33 skills break at
# cutover and the mistake is invisible in the diff.
#
# So this builds the real thing in miniature -- bind mounts, not symlinks,
# because a symlink would resolve `..` against the target and quietly give the
# on-disk answer for both cases -- and asks four questions:
#
#   1. does a vendored skill resolve through the farm symlink?
#   2. are ordinary runtime entrypoints present?
#   3. is any `~/.claude` spelling of a dev tree reachable, `..` climbs
#      included?
#   4. is the checkout's own `tests/` still readable by its real path?
#
# (4) is expected to be YES. That asymmetry is the property: controlled
# runtime deployment, not filesystem isolation. A probe that only asked (3)
# would read as proof of something this design does not deliver.
#
# Unprivileged, via bubblewrap, so it needs no root and touches nothing live.
# The vendored submodules are stubbed, because `git worktree add` does not
# initialise them and their contents are irrelevant here -- what is under test
# is path resolution.
#
# Run:  bash tests/mount-geometry-probe.sh
set -uo pipefail

REPO=$(git rev-parse --show-toplevel)
if [ ! -d "$REPO/runtime" ]; then
  echo "SKIP: no runtime/ in this tree -- run scripts/runtime-layout.py --apply first"
  exit 0
fi
if ! command -v bwrap >/dev/null 2>&1; then
  echo "SKIP: bubblewrap not available, so the mounts cannot be simulated"
  exit 0
fi

RIG=$(mktemp -d)
trap 'chmod -R u+w "$RIG" 2>/dev/null; command rm -rf -- "$RIG"' EXIT

mkdir -p "$RIG/mnt/runtime" \
         "$RIG/mnt/vendor/superpowers/skills" \
         "$RIG/mnt/vendor/everything-claude-code/skills" \
         "$RIG/stub/superpowers/skills/brainstorming" \
         "$RIG/stub/ecc/skills/accessibility"
echo SUPERPOWERS > "$RIG/stub/superpowers/skills/brainstorming/SKILL.md"
echo ECC         > "$RIG/stub/ecc/skills/accessibility/SKILL.md"

# `~/.claude/<entry>` is a declared symlink into the single runtime mount. This
# is the shape home-manager will write, so the probe uses it rather than
# mounting each entry separately.
for entry in skills hooks scripts agents commands; do
  ln -sfn "runtime/$entry" "$RIG/mnt/$entry"
done

cat > "$RIG/probe.sh" <<'PROBE'
set -u
C=$1; R=$2
fail=0
ok()   { printf '  ok    %-44s %s\n' "$1" "${2-}"; }
bad()  { printf '  FAIL  %-44s %s\n' "$1" "${2-}"; fail=1; }

echo "1. vendored skills through the farm symlink"
for s in brainstorming accessibility; do
  if [ -f "$C/skills/$s/SKILL.md" ]
    then ok "skills/$s" "-> $(cat "$C/skills/$s/SKILL.md")"
    else bad "skills/$s" "does not resolve under the mounts"
  fi
done

echo "2. ordinary runtime entrypoints"
for entry in hooks scripts agents commands; do
  if [ -d "$C/$entry" ] && [ -n "$(ls -A "$C/$entry" 2>/dev/null)" ]
    then ok "$entry/" "projected"
    else bad "$entry/" "empty or absent"
  fi
done

echo "3. dev trees, every spelling anchored under the config dir"
for tree in tests docs experiments research improvements; do
  for spell in "$C/$tree" "$C/runtime/$tree" "$C/hooks/../$tree" \
               "$C/scripts/../$tree" "$C/runtime/../$tree" \
               "$C/runtime/skills/../../$tree"; do
    if [ -e "$spell" ]
      then bad "${spell#"$C"/}" "reachable -- the projection leaks"
      else ok "${spell#"$C"/}" "absent"
    fi
  done
done

echo "4. the checkout itself, by its real path (expected: readable)"
if [ -d "$R/tests" ]
  then ok "<checkout>/tests" "readable -- by design; this is not isolation"
  else bad "<checkout>/tests" "missing, so the probe proves nothing about (4)"
fi

exit $fail
PROBE

bwrap --dev-bind / / \
  --bind "$REPO/runtime" "$RIG/mnt/runtime" \
  --bind "$RIG/stub/superpowers/skills" "$RIG/mnt/vendor/superpowers/skills" \
  --bind "$RIG/stub/ecc/skills" "$RIG/mnt/vendor/everything-claude-code/skills" \
  -- bash "$RIG/probe.sh" "$RIG/mnt" "$REPO"
status=$?

if [ "$status" -eq 0 ]
  then echo "mount-geometry-probe: the projected layout resolves, and the dev trees are not in it"
  else echo "mount-geometry-probe: FAILED" >&2
fi
exit "$status"
