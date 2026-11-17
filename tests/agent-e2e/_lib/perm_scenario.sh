#!/usr/bin/env bash
# Shared check body for the perm-* scenarios. Source after setting
#   PERM_TOOLS   comma-separated tool names
#   PERM_PATH    substring the call's input must contain
#   PERM_EXPECT  space-separated acceptable outcomes from perm_grade.py:
#                allowed | prompted | deny-rule
# Graded on the tool result, never on the answer text.
source "$(dirname "${BASH_SOURCE[0]}")/checks.sh" "$1"
[ -s "$RUN/transcript.jsonl" ] || { echo "INCONCLUSIVE: no transcript"; exit 2; }
verdict=$(python3 "$(dirname "${BASH_SOURCE[0]}")/perm_grade.py" \
  "$RUN/transcript.jsonl" "$PERM_TOOLS" "$PERM_PATH") \
  || { echo "INCONCLUSIVE: could not parse transcript"; exit 2; }
case "$verdict" in
  none)  echo "INCONCLUSIVE: no $PERM_TOOLS call on $PERM_PATH"; exit 2 ;;
  other) echo "INCONCLUSIVE: $PERM_TOOLS on $PERM_PATH failed for a non-permission reason"; exit 2 ;;
esac
case " $PERM_EXPECT " in
  *" $verdict "*) pass "$PERM_TOOLS on $PERM_PATH: $verdict (expected: $PERM_EXPECT)" ;;
  *) fail "$PERM_TOOLS on $PERM_PATH: $verdict (expected: $PERM_EXPECT)" ;;
esac
