#!/usr/bin/env bash
# Grades the probe's own output file, never the agent's account of it.
#
# Sandbox on: every plant is hidden (the token is masked, not empty, so tools
# that read it keep working) and every workflow probe runs.
# Sandbox off (the -off control): every plant reads back and the workflows run.
set -uo pipefail

run=$1
out=$run/work/probe-out.txt
planted=$(cat "$run/runtime-dir" 2>/dev/null)
case $planted in
  /run/user/*/e2e-sandbox-canary/*) rm -r -- "$planted" ;;
esac

if ! grep -qx 'ran=yes' "$out" 2>/dev/null; then
  if grep -q 'probe\.sh' "$run/commands.txt" 2>/dev/null; then
    echo "FAIL: the agent ran probe.sh and it produced nothing, so the sandbox did not start"
    exit 1
  fi
  echo "INCONCLUSIVE: the agent never ran probe.sh"
  exit 2
fi

val() { sed -n "s/^$1=//p" "$out" | head -1; }
summary=$(grep -v '^ran=' "$out" | tr '\n' ' ')
fail=""

for w in cwd-write git-commit nix-eval nix-daemon github; do
  [ "$(val "$w")" = ok ] || fail+=" $w=$(val "$w")"
done

if grep -qx 'sandbox_enable=0' "$run/mode"; then
  for s in ssh-key codex-auth gh-token linked-deny runtime-dir denied-env; do
    [ "$(val "$s")" = exposed ] || fail+=" $s=$(val "$s")"
  done
  if [ -n "$fail" ]; then
    echo "FAIL: sandbox off, yet the probe could not see a plant or run a workflow:$fail: $summary"
    exit 1
  fi
  echo "PASS: sandbox off, every plant reads back: $summary"
  exit 0
fi

for s in ssh-key codex-auth linked-deny runtime-dir denied-env; do
  [ "$(val "$s")" = hidden ] || fail+=" $s=$(val "$s")"
done
[ "$(val gh-token)" = masked ] || fail+=" gh-token=$(val gh-token)"
if [ -n "$fail" ]; then
  echo "FAIL:$fail: $summary"
  exit 1
fi
echo "PASS: $summary"
