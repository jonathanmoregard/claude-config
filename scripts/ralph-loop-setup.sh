#!/usr/bin/env bash

# Ralph Loop Setup (session-bound fork of ralph-loop@claude-plugins-official)
#
# Writes state to ~/.claude/ralph-loop/<session_id>.md so the Stop gate binds
# the session that armed it, regardless of which directory that session is
# working in, and binds no other session.

set -euo pipefail

STATE_DIR="$HOME/.claude/ralph-loop"

PROMPT_PARTS=()
MAX_ITERATIONS=0
COMPLETION_PROMISE="null"

while [[ $# -gt 0 ]]; do
  case $1 in
    -h|--help)
      cat << 'HELP_EOF'
Ralph Loop - Interactive self-referential development loop (session-bound)

USAGE:
  /ralph-loop [PROMPT...] [OPTIONS]

ARGUMENTS:
  PROMPT...    Initial prompt to start the loop (can be multiple words without quotes)

OPTIONS:
  --max-iterations <n>           Maximum iterations before auto-stop (default: unlimited)
  --completion-promise '<text>'  Promise phrase (USE QUOTES for multi-word)
  -h, --help                     Show this help message

DESCRIPTION:
  Starts a Ralph Loop bound to THIS session id. The stop hook prevents exit
  and feeds your output back as input until completion or iteration limit.

  State lives at ~/.claude/ralph-loop/<session_id>.md, so the gate follows
  the session (any cwd, any worktree) and never fires in a sibling session.

  To signal completion, you must output: <promise>YOUR_PHRASE</promise>

EXAMPLES:
  /ralph-loop Build a todo API --completion-promise 'DONE' --max-iterations 20
  /ralph-loop --max-iterations 10 Fix the auth bug
  /ralph-loop Refactor cache layer  (runs forever)

STOPPING:
  /cancel-ralph, reaching --max-iterations, or emitting --completion-promise.

MONITORING:
  head -10 ~/.claude/ralph-loop/$CLAUDE_CODE_SESSION_ID.md
HELP_EOF
      exit 0
      ;;
    --max-iterations)
      if [[ -z "${2:-}" ]]; then
        echo "❌ Error: --max-iterations requires a number argument" >&2
        echo "   Valid: --max-iterations 10 | 50 | 0 (unlimited)" >&2
        exit 1
      fi
      if ! [[ "$2" =~ ^[0-9]+$ ]]; then
        echo "❌ Error: --max-iterations must be a positive integer or 0, got: $2" >&2
        exit 1
      fi
      MAX_ITERATIONS="$2"
      shift 2
      ;;
    --completion-promise)
      if [[ -z "${2:-}" ]]; then
        echo "❌ Error: --completion-promise requires a text argument" >&2
        echo "   Multi-word promises must be quoted!" >&2
        exit 1
      fi
      COMPLETION_PROMISE="$2"
      shift 2
      ;;
    *)
      PROMPT_PARTS+=("$1")
      shift
      ;;
  esac
done

PROMPT="${PROMPT_PARTS[*]:-}"

if [[ -z "$PROMPT" ]]; then
  echo "❌ Error: No prompt provided" >&2
  echo "   Example: /ralph-loop Fix the auth bug --max-iterations 20" >&2
  echo "   For all options: /ralph-loop --help" >&2
  exit 1
fi

SESSION_ID="${CLAUDE_CODE_SESSION_ID:-}"
if [[ ! "$SESSION_ID" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "❌ Error: CLAUDE_CODE_SESSION_ID is unset or malformed (got: '${SESSION_ID}')" >&2
  echo "" >&2
  echo "   The loop is bound by session id. Without one there is nothing for" >&2
  echo "   the Stop hook to match, so the loop would silently never gate." >&2
  echo "   Refusing to arm." >&2
  exit 1
fi

mkdir -p "$STATE_DIR"
STATE_FILE="$STATE_DIR/$SESSION_ID.md"

# Quote completion promise for YAML if set
if [[ -n "$COMPLETION_PROMISE" ]] && [[ "$COMPLETION_PROMISE" != "null" ]]; then
  COMPLETION_PROMISE_YAML="\"$COMPLETION_PROMISE\""
else
  COMPLETION_PROMISE_YAML="null"
fi

cat > "$STATE_FILE" <<EOF
---
active: true
iteration: 1
session_id: $SESSION_ID
armed_in: "$(pwd)"
max_iterations: $MAX_ITERATIONS
completion_promise: $COMPLETION_PROMISE_YAML
started_at: "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
---

$PROMPT
EOF

cat <<EOF
🔄 Ralph loop activated for session $SESSION_ID

State file: $STATE_FILE
Iteration: 1
Max iterations: $(if [[ $MAX_ITERATIONS -gt 0 ]]; then echo $MAX_ITERATIONS; else echo "unlimited"; fi)
Completion promise: $(if [[ "$COMPLETION_PROMISE" != "null" ]]; then echo "${COMPLETION_PROMISE//\"/} (ONLY output when TRUE - do not lie!)"; else echo "none (runs forever)"; fi)

The gate binds THIS session id, not a directory. It fires wherever this
session stops, and never in another session. When you try to exit, the SAME
PROMPT is fed back to you.

To cancel: /cancel-ralph
EOF

echo ""
echo "$PROMPT"

if [[ "$COMPLETION_PROMISE" != "null" ]]; then
  echo ""
  echo "═══════════════════════════════════════════════════════════"
  echo "CRITICAL - Ralph Loop Completion Promise"
  echo "═══════════════════════════════════════════════════════════"
  echo ""
  echo "To complete this loop, output this EXACT text:"
  echo "  <promise>$COMPLETION_PROMISE</promise>"
  echo ""
  echo "STRICT REQUIREMENTS (DO NOT VIOLATE):"
  echo "  ✓ Use <promise> XML tags EXACTLY as shown above"
  echo "  ✓ The statement MUST be completely and unequivocally TRUE"
  echo "  ✓ Do NOT output false statements to exit the loop"
  echo "  ✓ Do NOT lie even if you think you should exit"
  echo ""
  echo "IMPORTANT - Do not circumvent the loop:"
  echo "  Even if you believe you're stuck, the task is impossible,"
  echo "  or you've been running too long - you MUST NOT output a"
  echo "  false promise statement. If the loop should stop, ask the"
  echo "  user or run /cancel-ralph. Do not force it by lying."
  echo "═══════════════════════════════════════════════════════════"
fi
