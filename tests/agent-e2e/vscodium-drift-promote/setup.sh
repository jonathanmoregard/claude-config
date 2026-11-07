#!/usr/bin/env bash
# Stages one nixos-drift-analyzer report whose ONLY finding is the VSCodium
# "live but not declared" class, plus the heartbeat that makes it trustworthy.
#
# Hermetic because XDG_DATA_HOME/XDG_STATE_HOME are unset in the harness env,
# so nixos-drift-health.py resolves $HOME/.local/{share,state} and $HOME is the
# throwaway staged home. If either variable is ever exported into a run, the
# hook would read tuxedo's real analyzer state instead: assert they are unset
# rather than silently grading the host's own drift.
#
# One finding and no other class, on purpose. The hook prints "Full report:
# <path>" whenever it speaks at all, so a report that also carried, say, a
# crontab finding would hand the old arm a path it could read the VSCodium
# section out of -- the arms would stop differing in what the agent CAN know.
# With only this finding, the old arm's allowlist matches nothing, the
# heartbeat count still agrees with what parsed, and the hook stays silent.
set -euo pipefail

work=$1
run=$2

for var in XDG_DATA_HOME XDG_STATE_HOME; do
  if [ -n "${!var:-}" ]; then
    echo "$var is set ($(eval echo "\$$var")); the run would read the real" \
         "analyzer state instead of the staged one" >&2
    exit 2
  fi
done

data=$HOME/.local/share/nixos-drift-analyzer
state=$HOME/.local/state/nixos-drift-analyzer
mkdir -p "$data" "$state" "$work"

# Sentinel keys: real VSCodium setting names, but ones with no reason to show
# up in an answer unless the hook put them in the agent's context.
cat >"$data/latest.md" <<'EOF'
# NixOS drift report

Generated: 2026-11-06T09:00:00+01:00

1 finding(s) need review.

## VSCodium settings live but not declared

~~~text
editor.stickyScroll.maxLineCount
terminal.integrated.scrollback

These survive reboot and deploy but not a fresh install. Promote by adding to
home/vscodium.nix in nixos-config; the finding clears once declared.
~~~

EOF

printf 'timestamp=%s\nfindings=1\n' "$(date -Iseconds)" >"$state/last-success"

# A plain workspace: the question is about the host, not about a repo.
git init -q "$work/notes"
git -C "$work/notes" -c user.name=e2e -c user.email=e2e@example.invalid \
  commit -q --allow-empty -m init

cp "$data/latest.md" "$run/staged-report.md"
