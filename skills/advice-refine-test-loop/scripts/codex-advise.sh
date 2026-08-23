#!/usr/bin/env bash
# Codex advisor invocation for advice-refine-test-loop.
#
# Two phases of the loop shell out to Codex — Phase 2 (cross-vendor correctness)
# and Phase 4b (second-vendor vision-taste). They share a flag set that is
# security-relevant and easy to get wrong: read-only sandbox, no --dangerously-*
# anything, a wall-clock cap, a schema that forces parseable output, and exactly
# one sanctioned retry per failure class. Keeping that in prose in two files
# invites drift — a flag added in one place, a timeout dropped in the other.
# This script is the single copy.
#
# What varies per call site stays at the call site: the prompt, the model, the
# images, the severity floor and the LOW filter. Those are judgement; this is
# plumbing.
#
# Usage:
#   codex-advise.sh --prompt-file <path> [--model <id>] [--images <csv>]
#                   [--timeout <secs>] [--out <path>] [--fallback-model <id>]
#
#   --prompt-file     File holding the prompt. Required. Passed verbatim.
#   --model           Model id. Default: whatever ~/.codex/config.toml selects.
#   --images          Comma-separated ABSOLUTE image paths for vision calls.
#                     Keep it to ~4 per call: Codex vision recall drops sharply
#                     past that (DiffSpot Hard-tier).
#   --timeout         Wall-clock cap in seconds. Default 300.
#   --out             Write the verdict JSON here. Default: stdout.
#   --fallback-model  Lighter tier tried once on quota exhaustion.
#                     Default gpt-5.4.
#
# Exit codes — every one of them is a distinct routing decision upstream:
#   0  a schema-shaped verdict was produced (.verdict is one of the three enum
#      values). The parent still runs step 4's coherence cross-validation; this
#      script does not, because that check applies to the Opus lane too and
#      must not appear to be Codex-only.
#   2  the script could not run: missing codex or jq, bad arguments, unreadable
#      prompt file. Not a review result. Never treat as a pass.
#   3  quota exhausted, including after the one fallback-model retry.
#   4  timeout or connectivity failure, including after the one 30s retry.
#   5  output was not parseable as a verdict, including after the one re-ask.
#
# 3, 4 and 5 are the loop's documented PAUSE triggers. This script never decides
# to continue without a verdict, and never falls back to a third attempt.

set -uo pipefail

SCHEMA="${HOME}/.claude/skills/advice-refine-test-loop/verdict-schema.json"

PROMPT_FILE=""
MODEL=""
IMAGES=""
TIMEOUT=300
OUT=""
FALLBACK_MODEL="gpt-5.4"

# Refuse sandbox-escaping flags outright. The loop's guarantee that Codex never
# edits rests on this; a caller that wants it has misread the skill.
for arg in "$@"; do
  case "$arg" in
    --dangerously*|--full-auto|--yolo)
      echo "codex-advise: refusing sandbox-escaping flag: $arg" >&2
      echo "codex-advise: Codex is a read-only reviewer in this loop; the parent writes every fix." >&2
      exit 2
      ;;
  esac
done

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prompt-file)    PROMPT_FILE="${2:-}"; shift 2 ;;
    --model)          MODEL="${2:-}"; shift 2 ;;
    --images)         IMAGES="${2:-}"; shift 2 ;;
    --timeout)        TIMEOUT="${2:-}"; shift 2 ;;
    --out)            OUT="${2:-}"; shift 2 ;;
    --fallback-model) FALLBACK_MODEL="${2:-}"; shift 2 ;;
    -h|--help)        sed -n '2,44p' "$0"; exit 0 ;;
    *) echo "codex-advise: unknown argument: $1" >&2; exit 2 ;;
  esac
done

command -v codex >/dev/null 2>&1 || { echo "codex-advise: codex not on PATH" >&2; exit 2; }
command -v jq    >/dev/null 2>&1 || { echo "codex-advise: jq not on PATH" >&2; exit 2; }
[[ -n "$PROMPT_FILE" ]] || { echo "codex-advise: --prompt-file is required" >&2; exit 2; }
[[ -r "$PROMPT_FILE" ]] || { echo "codex-advise: cannot read prompt file: $PROMPT_FILE" >&2; exit 2; }
[[ -r "$SCHEMA" ]]      || { echo "codex-advise: cannot read schema: $SCHEMA" >&2; exit 2; }
[[ "$TIMEOUT" =~ ^[0-9]+$ ]] || { echo "codex-advise: --timeout must be an integer" >&2; exit 2; }

if [[ -n "$IMAGES" ]]; then
  IFS=',' read -r -a _shots <<< "$IMAGES"
  for shot in "${_shots[@]}"; do
    [[ "$shot" == /* ]] || { echo "codex-advise: image path must be absolute: $shot" >&2; exit 2; }
    [[ -r "$shot" ]]    || { echo "codex-advise: cannot read image: $shot" >&2; exit 2; }
  done
  if (( ${#_shots[@]} > 4 )); then
    echo "codex-advise: warning: ${#_shots[@]} images in one call; recall drops sharply past 4" >&2
  fi
fi

ERRFILE="$(mktemp)"
trap 'rm -f "$ERRFILE"' EXIT

RESPONSE=""
STATUS=""

# One attempt. Sets RESPONSE and STATUS; STATUS is one of:
#   ok | quota | stalled | unparseable
attempt() {
  local model="$1" prompt_suffix="${2:-}"
  local -a cmd=(codex exec --skip-git-repo-check --output-schema "$SCHEMA")
  [[ -n "$model"  ]] && cmd+=(-m "$model")
  [[ -n "$IMAGES" ]] && cmd+=(--image "$IMAGES")

  local prompt rc out
  prompt="$(cat "$PROMPT_FILE")${prompt_suffix}"

  : >"$ERRFILE"
  out="$(timeout "${TIMEOUT}s" "${cmd[@]}" "$prompt" 2>"$ERRFILE")"
  rc=$?

  local diagnostics
  diagnostics="$out$(cat "$ERRFILE")"

  if (( rc == 124 )); then STATUS="stalled"; return; fi

  if grep -qiE 'usage limit|quota exceeded|rate limit|\b429\b' <<<"$diagnostics"; then
    STATUS="quota"; return
  fi

  if (( rc != 0 )); then STATUS="stalled"; return; fi

  local verdict
  verdict="$(jq -r '.verdict? // empty' <<<"$out" 2>/dev/null)"
  if [[ "$verdict" =~ ^(PASS|PASS_WITH_FIXES|BLOCKS)$ ]]; then
    RESPONSE="$out"; STATUS="ok"; return
  fi

  STATUS="unparseable"
}

emit() {
  if [[ -n "$OUT" ]]; then
    printf '%s\n' "$RESPONSE" >"$OUT"
  else
    printf '%s\n' "$RESPONSE"
  fi
}

attempt "$MODEL"

case "$STATUS" in
  ok) emit; exit 0 ;;

  quota)
    echo "codex-advise: quota exhausted on ${MODEL:-default}; retrying once on $FALLBACK_MODEL" >&2
    attempt "$FALLBACK_MODEL"
    case "$STATUS" in
      ok)          emit; exit 0 ;;
      quota)       echo "codex-advise: quota exhausted on fallback too" >&2; exit 3 ;;
      stalled)     echo "codex-advise: fallback stalled" >&2; exit 4 ;;
      unparseable) echo "codex-advise: fallback produced no parseable verdict" >&2; exit 5 ;;
    esac
    ;;

  stalled)
    echo "codex-advise: no response within ${TIMEOUT}s or connectivity failure; one retry in 30s" >&2
    sleep 30
    attempt "$MODEL"
    case "$STATUS" in
      ok)          emit; exit 0 ;;
      quota)       echo "codex-advise: quota exhausted on retry" >&2; exit 3 ;;
      stalled)     echo "codex-advise: stalled again" >&2; exit 4 ;;
      unparseable) echo "codex-advise: retry produced no parseable verdict" >&2; exit 5 ;;
    esac
    ;;

  unparseable)
    echo "codex-advise: no parseable verdict; one re-ask with an explicit schema reminder" >&2
    attempt "$MODEL" $'\n\nYour last output was not valid JSON matching verdict-schema.json. Emit exactly that shape and nothing else: no prose outside the JSON object.'
    case "$STATUS" in
      ok)          emit; exit 0 ;;
      quota)       echo "codex-advise: quota exhausted on re-ask" >&2; exit 3 ;;
      stalled)     echo "codex-advise: re-ask stalled" >&2; exit 4 ;;
      unparseable) echo "codex-advise: re-ask produced no parseable verdict either" >&2; exit 5 ;;
    esac
    ;;
esac

echo "codex-advise: unreachable state: $STATUS" >&2
exit 2
