#!/usr/bin/env bash
# A ticket-routing script that asks `claude -p` to classify a ticket and has a
# TODO where it reads the answer. A `claude` stub in $E2E_BIN logs every call
# and answers in the shape asked: JSON with structured_output under
# --json-schema, a JSON envelope under --output-format json, else prose.
set -euo pipefail
WORK="${E2E_WORK:?}"
git -C "$WORK" init -q -b main
git -C "$WORK" config user.email e2e@example.invalid
git -C "$WORK" config user.name e2e
mkdir -p "$WORK/scripts" "$WORK/tickets"

cat > "$WORK/scripts/triage.sh" <<'SH'
#!/usr/bin/env bash
# Route a support ticket: ask Claude whether it is a bug report or a feature
# request, then file it in the matching queue.
# usage: scripts/triage.sh tickets/<file>
set -euo pipefail
ticket_file="$1"
answer=$(claude -p "Is this support ticket a bug report or a feature request?

$(cat "$ticket_file")")
# TODO: set CATEGORY to bug or feature from $answer
CATEGORY=""
case "$CATEGORY" in
  bug) queue=bugs ;;
  feature) queue=features ;;
  *) echo "could not classify: $answer" >&2; exit 1 ;;
esac
mkdir -p "queues/$queue"
cp "$ticket_file" "queues/$queue/"
echo "filed $(basename "$ticket_file") in $queue"
SH
chmod +x "$WORK/scripts/triage.sh"

cat > "$WORK/tickets/1042.txt" <<'TXT'
Subject: Export button does nothing
Clicking "Export CSV" on the reports page shows a spinner and then nothing
downloads. Worked last week. Not a feature request, this is broken.
TXT
cat > "$WORK/tickets/1043.txt" <<'TXT'
Subject: Dark mode please
It would be great to have a dark theme. No bug here, just a wish.
TXT
git -C "$WORK" add -A
git -C "$WORK" commit -qm "triage script skeleton"

# The stub answers like the real CLI: structured_output only with both
# --output-format json and --json-schema (either flag form), a JSON envelope
# with prose for --output-format json alone, prose otherwise. Its category
# follows the ticket it is given (the dark-mode ticket is a feature).
{
  printf '#!/usr/bin/env bash\nCALLS=%q\n' "$E2E_RUN/claude-calls.txt"
  cat <<'SH'
printf '%s\n' "$*" >> "$CALLS"
json=0 schema=0 prompt=""
while [ $# -gt 0 ]; do
  case "$1" in
    --output-format) [ "${2:-}" = json ] && json=1; shift 2 ;;
    --output-format=json) json=1; shift ;;
    --json-schema) schema=1; shift 2 ;;
    --json-schema=*) schema=1; shift ;;
    --model|--tools|--max-turns|--system-prompt|--append-system-prompt) shift 2 ;;
    -*) shift ;;
    *) prompt="$prompt $1"; shift ;;
  esac
done
[ -t 0 ] || prompt="$prompt $(cat)"
case "$prompt" in
  *"Dark mode"*) category=feature; text="This one is a feature request, not a bug report." ;;
  *) category=bug; text="This one is a bug report, not a feature request." ;;
esac
if [ "$json" = 1 ] && [ "$schema" = 1 ]; then
  printf '{"type":"result","is_error":false,"result":"","structured_output":{"category":"%s"}}\n' "$category"
elif [ "$json" = 1 ]; then
  printf '{"type":"result","is_error":false,"result":"%s"}\n' "$text"
else
  printf '%s\n' "$text"
fi
SH
} > "$E2E_BIN/claude"
chmod +x "$E2E_BIN/claude"
