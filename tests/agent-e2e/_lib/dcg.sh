#!/usr/bin/env bash
# Staging helpers for the dcg rule-set health scenarios. $HOME is the sandbox
# HOME the run was staged into, so both of these write where the SessionStart
# hook will look: the marker under $HOME/.local/state, dcg's config where
# `~/.config/dcg/config.toml` resolves.

# nixos-config's activation marker, in the shape home/dcg.nix:434 writes it.
# `observed_at` is deliberately old: the false alarm these scenarios are about
# came from a marker left behind by the 2026-10-29 runtime-layout cutover and
# never cleaned up, because home-manager only deletes it on an activation that
# finds the fallback gone.
write_dcg_marker() {
  local marker="$HOME/.local/state/dcg/fallback-active.json"
  mkdir -p "$(dirname "$marker")"
  cat > "$marker" <<JSON
{
  "schema": 1,
  "component": "dcg",
  "state": "fallback-active",
  "target": "$HOME/.claude/dcg.toml",
  "fallback_source": "/nix/store/zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz-dcg-fallback.toml",
  "fallback_sha256": "e55b000000000000000000000000000000000000000000000000000000000000",
  "observed_at": "2026-10-29T08:14:03Z",
  "remedy": "restore ~/.claude/dcg.toml and re-activate home-manager"
}
JSON
}

# Put the REAL bootstrap seed where dcg loads from, so a scenario that wants a
# live fallback gets one from dcg's own loader rather than from a stub's
# say-so. The harness copies the real ~/.config/dcg/config.toml into the
# sandbox; this replaces that copy. Absent seed -> nonzero, which the harness
# reports as INCONCLUSIVE rather than letting the run grade a state it never
# reached.
seed_dcg_fallback() {
  local seed=/etc/nixos/home/dcg-fallback.toml
  if [ ! -r "$seed" ]; then
    echo "no dcg bootstrap seed at $seed (nixos-config not checked out there?)" >&2
    return 1
  fi
  mkdir -p "$HOME/.config/dcg"
  cp "$seed" "$HOME/.config/dcg/config.toml"
  grep -q 'BOOTSTRAP FALLBACK' "$HOME/.config/dcg/config.toml"
}

# Make the sandbox look like a host the runtime projection is live on. The
# marker is untracked state in ~/.claude, so it is not part of any staged
# config arm and a run is a NON-projected host unless this is called -- which
# matters because the announcement's FIX line branches on exactly this file.
# Measured without it: 2 of 5 runs relayed the flat-checkout branch, 0 of 5
# the projected one, which was the right read of the host they were given and
# the wrong one for the host this config ships to.
mark_projected_host() {
  : > "$HOME/.claude/.projection-enabled"
}

# A plain workspace. These scenarios are about what session start says, not
# about anything in the tree, so the repo stays deliberately boring: a tree
# with finished work in it would give the agent a second thing to report and
# muddy the grade.
make_plain_repo() {
  local work="${E2E_WORK:?}"
  printf 'notes\n=====\n\nnothing started yet.\n' > "$work/README.md"
  git -C "$work" init -q
  git -C "$work" add -A
  git -C "$work" commit -qm init
}
