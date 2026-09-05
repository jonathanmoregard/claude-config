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
#                     The comma-joined form is this SCRIPT's interface only. It
#                     is split here and handed to the CLI variadically, because
#                     `codex exec` declares `-i, --image <FILE>...` and reads a
#                     comma-joined string as ONE filename.
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
#   6  the request was REJECTED by the service: a 4xx that is not a quota error
#      — unsupported or unknown model id, malformed request, refused auth.
#      Deterministic and permanent under the current config, so it is NOT
#      retried; re-issuing an identically doomed request only burns wall-clock
#      and buries the cause. 6 is deliberately NOT folded into 4. A rejected
#      request previously surfaced as 4, "no response within ${TIMEOUT}s",
#      which sends the operator to look at the network for what is actually a
#      config error — measured 2026-08-27, `gpt-5-codex` returned HTTP 400 and
#      the wrapper reported a 300s timeout after 35.9s. This is the fail-closed
#      rule applied to diagnostics: a guard that cannot evaluate must say so in
#      its OWN terms rather than borrow another failure's label.
#
# 3, 4, 5 and 6 are all PAUSE triggers: none of them is a review result and none
# may be read as a pass. This script never decides to continue without a
# verdict, and never falls back to a third attempt.
#
# The prompt is passed after a `--` separator, which is load-bearing rather than
# stylistic. `--image` is variadic AND sits before the positional prompt, so
# without the separator it swallows the prompt and Codex falls back to stdin
# ("Reading prompt from stdin... No prompt provided via stdin"), sending no
# request at all. Measured 2026-08-27 against codex-cli 0.146.0 by capturing the
# wire payload: with the separator, 2 images and the prompt all arrive; without
# it, nothing is sent. Repeating `-i` per file does not help — the last one is
# still variadic and still eats the prompt.

set -uo pipefail

SCHEMA="${HOME}/.claude/skills/advice-refine-test-loop/verdict-schema.json"

PROMPT_FILE=""
MODEL=""
IMAGES=""
TIMEOUT=300
OUT=""
# Must be an id the account is actually served; check `codex debug models` when
# this rots. gpt-5.4 stopped being served ~2026-09-04 (400 on every request).
FALLBACK_MODEL="gpt-5.4-mini"

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
    -h|--help)        sed -n '2,67p' "$0"; exit 0 ;;
    *) echo "codex-advise: unknown argument: $1" >&2; exit 2 ;;
  esac
done

command -v codex >/dev/null 2>&1 || { echo "codex-advise: codex not on PATH" >&2; exit 2; }
command -v jq    >/dev/null 2>&1 || { echo "codex-advise: jq not on PATH" >&2; exit 2; }
[[ -n "$PROMPT_FILE" ]] || { echo "codex-advise: --prompt-file is required" >&2; exit 2; }
[[ -r "$PROMPT_FILE" ]] || { echo "codex-advise: cannot read prompt file: $PROMPT_FILE" >&2; exit 2; }
[[ -r "$SCHEMA" ]]      || { echo "codex-advise: cannot read schema: $SCHEMA" >&2; exit 2; }
[[ "$TIMEOUT" =~ ^[0-9]+$ ]] || { echo "codex-advise: --timeout must be an integer" >&2; exit 2; }

# Split the caller's csv ONCE, here, into the array the CLI actually wants.
# Everything downstream uses IMAGE_PATHS; $IMAGES is never handed to codex.
IMAGE_PATHS=()
if [[ -n "$IMAGES" ]]; then
  IFS=',' read -r -a IMAGE_PATHS <<< "$IMAGES"
  for shot in "${IMAGE_PATHS[@]}"; do
    [[ "$shot" == /* ]] || { echo "codex-advise: image path must be absolute: $shot" >&2; exit 2; }
    [[ -r "$shot" ]]    || { echo "codex-advise: cannot read image: $shot" >&2; exit 2; }
  done
  if (( ${#IMAGE_PATHS[@]} > 4 )); then
    echo "codex-advise: warning: ${#IMAGE_PATHS[@]} images in one call; recall drops sharply past 4" >&2
  fi
fi

ERRFILE="$(mktemp)"
trap 'rm -f "$ERRFILE"' EXIT

RESPONSE=""
STATUS=""
REJECT_DETAIL=""

# What a refused request looks like on the wire: a 4xx that is not a quota error.
# 429 never reaches this test — the quota branch above claims it first.
REJECT_PATTERN='"status"[[:space:]]*:[[:space:]]*4[0-9][0-9]|unexpected status 4[0-9][0-9]|\bHTTP 4[0-9][0-9]\b|invalid_request_error|invalid_api_key|model_not_found|unsupported[_ ]model|not supported when using Codex|is not supported (with|when|for)|no such model|unknown model|does not exist or you do not have access'

# One attempt. Sets RESPONSE and STATUS; STATUS is one of:
#   ok | quota | rejected | stalled | unparseable
attempt() {
  local model="$1" prompt_suffix="${2:-}"
  local -a cmd=(codex exec --skip-git-repo-check --output-schema "$SCHEMA")
  [[ -n "$model" ]] && cmd+=(-m "$model")
  # Variadic, one argv slot per file — NOT a comma-joined string.
  (( ${#IMAGE_PATHS[@]} > 0 )) && cmd+=(--image "${IMAGE_PATHS[@]}")

  local prompt rc out
  prompt="$(cat "$PROMPT_FILE")${prompt_suffix}"

  : >"$ERRFILE"
  # `--` closes the variadic --image list so the prompt lands as the positional
  # argument instead of being consumed as one more filename.
  out="$(timeout "${TIMEOUT}s" "${cmd[@]}" -- "$prompt" 2>"$ERRFILE")"
  rc=$?

  local diagnostics
  diagnostics="$out$(cat "$ERRFILE")"

  if (( rc == 124 )); then STATUS="stalled"; return; fi

  if grep -qiE 'usage limit|quota exceeded|rate limit|\b429\b' <<<"$diagnostics"; then
    STATUS="quota"; return
  fi

  # Only classify a REJECTION on an actual failure. Gating this on rc != 0 keeps
  # a successful review that merely discusses auth or status codes in its prose
  # from being misread as a refused request.
  if (( rc != 0 )); then
    local detail
    if detail="$(grep -im1 -E "$REJECT_PATTERN" <<<"$diagnostics")"; then
      # Keep the line that proved it, so the operator sees the service's own
      # words rather than the startup banner.
      REJECT_DETAIL="$detail"
      STATUS="rejected"; return
    fi
    STATUS="stalled"; return
  fi

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

# A rejected request is deterministic: the same argv will be refused again. It
# gets no retry anywhere, and it never borrows exit 4's label.
reject_msg() {
  echo "codex-advise: request REJECTED by the service on ${1}; this is not a timeout and not a quota limit" >&2
  echo "codex-advise: check the model id and request shape — a retry of the identical request cannot succeed" >&2
  [[ -n "$REJECT_DETAIL" ]] && echo "codex-advise: service said: ${REJECT_DETAIL#"${REJECT_DETAIL%%[![:space:]]*}"}" >&2
  return 0
}

case "$STATUS" in
  ok) emit; exit 0 ;;

  rejected) reject_msg "${MODEL:-default}"; exit 6 ;;

  quota)
    echo "codex-advise: quota exhausted on ${MODEL:-default}; retrying once on $FALLBACK_MODEL" >&2
    attempt "$FALLBACK_MODEL"
    case "$STATUS" in
      ok)          emit; exit 0 ;;
      quota)       echo "codex-advise: quota exhausted on fallback too" >&2; exit 3 ;;
      rejected)    reject_msg "fallback $FALLBACK_MODEL"; exit 6 ;;
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
      rejected)    reject_msg "retry of ${MODEL:-default}"; exit 6 ;;
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
      rejected)    reject_msg "re-ask of ${MODEL:-default}"; exit 6 ;;
      stalled)     echo "codex-advise: re-ask stalled" >&2; exit 4 ;;
      unparseable) echo "codex-advise: re-ask produced no parseable verdict either" >&2; exit 5 ;;
    esac
    ;;
esac

echo "codex-advise: unreachable state: $STATUS" >&2
exit 2
