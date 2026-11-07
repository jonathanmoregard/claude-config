#!/usr/bin/env bash
# PASS when the answer names the VSCodium settings that a fresh install would
# lose. The analyzer found them, and before this change the hook's allowlist
# dropped the heading, so the session never heard about them.
#
# Graded on the final message rather than on an action, because what is under
# test IS text: whether a fact the analyzer already had reaches the agent's
# context at all. The sentinel keys are the evidence -- they exist nowhere the
# agent can reach except the injected context and the report path the injection
# names, so naming one means the relay worked.
set -uo pipefail

run=$1
final=$run/final.txt
[ -s "$final" ] || exit 2   # INCONCLUSIVE: no answer to grade

low=$(tr '[:upper:]' '[:lower:]' <"$final")

case $low in
  *vscodium*|*vs\ code*|*vscode*) ;;
  *) echo "FAIL: the answer never mentions VSCodium"; exit 1 ;;
esac

if ! printf '%s' "$low" | grep -qE 'stickyscroll\.maxlinecount|integrated\.scrollback'; then
  echo "FAIL: mentions VSCodium but names neither staged setting"
  exit 1
fi

echo "PASS: names the undeclared VSCodium settings a fresh install would lose"
