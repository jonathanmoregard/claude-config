#!/usr/bin/env bash
# Fixture suite for codex-advise.sh.
#
# Every case runs against a STUBBED codex on PATH. Nothing here reaches the
# network, a real model, or a quota. The point is the routing: each failure
# class must produce its own exit code, because upstream those codes are
# different decisions and a collapsed one would silently ship a phase that
# never ran.
#
# Usage: codex-advise-test.sh    Exit: 0 all pass, 1 any fail.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUT="$HERE/codex-advise.sh"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

STUB_DIR="$WORK/bin"
mkdir -p "$STUB_DIR"
PROMPT="$WORK/prompt.txt"
printf 'review this diff\n' >"$PROMPT"

# ── the stub ─────────────────────────────────────────────────────────────────
cat >"$STUB_DIR/codex" <<'STUB'
#!/usr/bin/env bash
n_file="${CODEX_STUB_COUNT:-/dev/null}"
n=0
[[ -f "$n_file" ]] && n="$(cat "$n_file")"
n=$((n + 1))
[[ "$n_file" != /dev/null ]] && printf '%s' "$n" >"$n_file"
[[ -n "${CODEX_STUB_ARGS:-}" ]] && printf '%s\n' "$*" >>"$CODEX_STUB_ARGS"
# Per-argument capture. "$*" flattens argv, so a single argument "a.png b.png"
# is indistinguishable from two arguments there — exactly the bug under test.
# One line per argv slot is the only way to assert variadic passing.
if [[ -n "${CODEX_STUB_ARGV:-}" ]]; then
  { printf '<<<CALL>>>\n'; printf '%s\n' "$@"; } >>"$CODEX_STUB_ARGV"
fi

emit_ok() { printf '%s\n' '{"verdict":"PASS","verdict_reason":"nothing found","findings":[]}'; }

# The verbatim envelope measured 2026-08-27 from `codex exec -m gpt-5-codex`
# under ChatGPT auth. Kept literal so the classifier is tested against the real
# wire text, not against a paraphrase of it.
emit_rejected() {
  echo 'ERROR: {"type":"error","status":400,"error":{"type":"invalid_request_error","message":"The '"'"'gpt-5-codex'"'"' model is not supported when using Codex with a ChatGPT account."}}' >&2
  exit 1
}

case "${CODEX_STUB_MODE:-ok}" in
  ok)                emit_ok ;;
  quota)             echo "stream error: usage limit reached" >&2; exit 1 ;;
  quota_then_ok)     if (( n == 1 )); then echo "usage limit reached" >&2; exit 1; else emit_ok; fi ;;
  unparseable)       echo "Looks fine to me, no major issues." ;;
  unparseable_then_ok) if (( n == 1 )); then echo "Looks fine, SAFE-ish"; else emit_ok; fi ;;
  wrong_verdict)     printf '%s\n' '{"verdict":"LGTM","findings":[]}' ;;
  stall)             sleep 5 ;;
  rejected)          emit_rejected ;;
  quota_then_rejected) if (( n == 1 )); then echo "usage limit reached" >&2; exit 1; else emit_rejected; fi ;;
  # A clean verdict whose PROSE happens to discuss 4xx / auth vocabulary. Must
  # stay exit 0: the rejected classifier keys on a failed call, not on words.
  ok_mentions_4xx)
    printf '%s\n' '{"verdict":"PASS_WITH_FIXES","verdict_reason":"the handler returns HTTP 400 invalid_request_error for an unknown model id; that branch needs a test","findings":[]}' ;;
esac
STUB
chmod +x "$STUB_DIR/codex"

PASS=0
FAIL=0

# expect <name> <expected-exit> <mode> [extra args...]
expect() {
  local name="$1" want="$2" mode="$3"; shift 3
  local count_file="$WORK/count.$RANDOM"
  : >"$count_file"
  local got
  PATH="$STUB_DIR:$PATH" CODEX_STUB_MODE="$mode" CODEX_STUB_COUNT="$count_file" \
    "$SUT" --prompt-file "$PROMPT" "$@" >"$WORK/out.txt" 2>"$WORK/err.txt"
  got=$?
  if [[ "$got" == "$want" ]]; then
    printf '  PASS  %-46s exit %s\n' "$name" "$got"
    PASS=$((PASS + 1))
  else
    printf '  FAIL  %-46s want %s got %s\n' "$name" "$want" "$got"
    printf '        stderr: %s\n' "$(head -2 "$WORK/err.txt")"
    FAIL=$((FAIL + 1))
  fi
}

echo "codex-advise.sh fixtures"
echo

expect "valid verdict"                        0 ok
expect "quota, fallback succeeds"             0 quota_then_ok
expect "quota on both tiers"                  3 quota
expect "unparseable, re-ask succeeds"         0 unparseable_then_ok
expect "unparseable twice"                    5 unparseable
expect "verdict outside the enum"             5 wrong_verdict
expect "stall past the cap"                   4 stall --timeout 1

echo
echo "rejected request is its own class, never a timeout"
expect "rejected request (HTTP 400)"          6 rejected
expect "rejected on the fallback tier"        6 quota_then_rejected
expect "4xx vocabulary in a CLEAN verdict"    0 ok_mentions_4xx

# A rejected request is deterministic. Retrying it burns wall-clock and buries
# the cause — the measured failure spent a 30s sleep re-issuing a doomed call.
REJ_ARGS="$WORK/args.rejected"
: >"$REJ_ARGS"
rej_count="$WORK/count.rejected"
: >"$rej_count"
PATH="$STUB_DIR:$PATH" CODEX_STUB_MODE=rejected CODEX_STUB_COUNT="$rej_count" \
  CODEX_STUB_ARGS="$REJ_ARGS" "$SUT" --prompt-file "$PROMPT" >/dev/null 2>&1
if [[ "$(wc -l <"$REJ_ARGS")" == 1 ]]; then
  printf '  PASS  %-46s\n' "rejected is never retried"; PASS=$((PASS + 1))
else
  printf '  FAIL  %-46s got %s calls\n' "rejected is never retried" "$(wc -l <"$REJ_ARGS")"
  FAIL=$((FAIL + 1))
fi

# The operator must not be sent to look at the network for a config error.
PATH="$STUB_DIR:$PATH" CODEX_STUB_MODE=rejected \
  "$SUT" --prompt-file "$PROMPT" >/dev/null 2>"$WORK/rej.err"
if grep -qi 'REJECTED' "$WORK/rej.err" && ! grep -qiE 'no response within|connectivity' "$WORK/rej.err"; then
  printf '  PASS  %-46s\n' "rejection is not worded as a timeout"; PASS=$((PASS + 1))
else
  printf '  FAIL  %-46s\n' "rejection is not worded as a timeout"
  printf '        stderr: %s\n' "$(head -2 "$WORK/rej.err")"
  FAIL=$((FAIL + 1))
fi

echo
echo "argument guards"
expect "sandbox-escape flag refused"          2 ok --dangerously-bypass-approvals
expect "non-absolute image path"              2 ok --images "relative/shot.png"
expect "unreadable image"                     2 ok --images "/nonexistent/shot.png"
expect "non-integer timeout"                  2 ok --timeout abc
expect "unknown argument"                     2 ok --wat

# missing prompt file needs its own invocation shape
PATH="$STUB_DIR:$PATH" CODEX_STUB_MODE=ok "$SUT" --prompt-file /nonexistent/p.txt >/dev/null 2>&1
if [[ $? == 2 ]]; then
  printf '  PASS  %-46s exit 2\n' "unreadable prompt file"; PASS=$((PASS + 1))
else
  printf '  FAIL  %-46s want 2\n' "unreadable prompt file"; FAIL=$((FAIL + 1))
fi

echo
echo "invariant flags reach codex on every call"
ARGS_FILE="$WORK/args.txt"
: >"$ARGS_FILE"
count_file="$WORK/count.flags"
: >"$count_file"
PATH="$STUB_DIR:$PATH" CODEX_STUB_MODE=quota_then_ok CODEX_STUB_COUNT="$count_file" \
  CODEX_STUB_ARGS="$ARGS_FILE" "$SUT" --prompt-file "$PROMPT" >/dev/null 2>&1

check_all_lines() {
  local label="$1" pattern="$2" want_present="$3"
  local bad=0 line
  while IFS= read -r line; do
    if [[ "$want_present" == yes ]]; then
      grep -q -- "$pattern" <<<"$line" || bad=1
    else
      grep -q -- "$pattern" <<<"$line" && bad=1
    fi
  done <"$ARGS_FILE"
  if (( bad == 0 )); then
    printf '  PASS  %-46s\n' "$label"; PASS=$((PASS + 1))
  else
    printf '  FAIL  %-46s\n' "$label"; FAIL=$((FAIL + 1))
  fi
}

check_all_lines "--skip-git-repo-check on every call" "skip-git-repo-check" yes
check_all_lines "--output-schema on every call"       "output-schema" yes
check_all_lines "no --dangerously-* ever"             "dangerously" no
check_all_lines "no --full-auto ever"                 "full-auto" no

if grep -q -- "-m gpt-5.4" "$ARGS_FILE"; then
  printf '  PASS  %-46s\n' "fallback tier used on retry"; PASS=$((PASS + 1))
else
  printf '  FAIL  %-46s\n' "fallback tier used on retry"; FAIL=$((FAIL + 1))
fi

if [[ "$(wc -l <"$ARGS_FILE")" == 2 ]]; then
  printf '  PASS  %-46s\n' "exactly one retry, never a third"; PASS=$((PASS + 1))
else
  printf '  FAIL  %-46s got %s calls\n' "exactly one retry, never a third" "$(wc -l <"$ARGS_FILE")"
  FAIL=$((FAIL + 1))
fi

echo
echo "image argv construction (the multi-image case that never worked)"

# `codex exec` declares `-i, --image <FILE>...` — VARIADIC — and the prompt is a
# POSITIONAL that follows it. Two things therefore have to hold, and neither is
# visible in a flattened "$*": each path needs its own argv slot, and a `--` has
# to close the list or the variadic flag eats the prompt as one more filename.
# Measured against codex-cli 0.146.0 on 2026-08-27 by capturing the wire
# payload: with `--`, 2 images and the prompt arrive; without it, codex falls
# back to stdin and sends no request at all.
SHOT1="$WORK/shot one.png"   # a space, to catch word-splitting regressions too
SHOT2="$WORK/shot2.png"
printf 'not-really-a-png\n' >"$SHOT1"
printf 'not-really-a-png\n' >"$SHOT2"

ARGV_FILE="$WORK/argv.images"
: >"$ARGV_FILE"
argv_count="$WORK/count.images"
: >"$argv_count"
PATH="$STUB_DIR:$PATH" CODEX_STUB_MODE=ok CODEX_STUB_COUNT="$argv_count" \
  CODEX_STUB_ARGV="$ARGV_FILE" "$SUT" --prompt-file "$PROMPT" \
  --images "$SHOT1,$SHOT2" >/dev/null 2>&1

argv_has() { grep -qxF -- "$1" "$ARGV_FILE"; }
argv_idx()  { grep -nxF -- "$1" "$ARGV_FILE" | head -1 | cut -d: -f1; }

check() {
  local label="$1" cond="$2"
  if [[ "$cond" == ok ]]; then
    printf '  PASS  %-46s\n' "$label"; PASS=$((PASS + 1))
  else
    printf '  FAIL  %-46s\n' "$label"; FAIL=$((FAIL + 1))
  fi
}

# Each image is its own argument.
argv_has "$SHOT1" && r=ok || r=no
check "first image is its own argv slot" "$r"
argv_has "$SHOT2" && r=ok || r=no
check "second image is its own argv slot" "$r"

# The exact original bug: both paths fused into one comma-joined argument, which
# the CLI reads as a single filename.
grep -qF -- "$SHOT1,$SHOT2" "$ARGV_FILE" && r=no || r=ok
check "paths never fused into one comma argument" "$r"

# Exactly one --image flag introducing the variadic list.
[[ "$(grep -cxF -- '--image' "$ARGV_FILE")" == 1 ]] && r=ok || r=no
check "one --image flag introduces the list" "$r"

# The separator exists and closes the list after the images.
argv_has -- "--" && r=ok || r=no
check "-- separator present" "$r"

sep="$(argv_idx '--')"; i1="$(argv_idx "$SHOT1")"; i2="$(argv_idx "$SHOT2")"
prompt_idx="$(grep -nxF -- 'review this diff' "$ARGV_FILE" | head -1 | cut -d: -f1)"
if [[ -n "$sep" && -n "$i1" && -n "$i2" && "$sep" -gt "$i1" && "$sep" -gt "$i2" ]]; then
  r=ok; else r=no; fi
check "-- closes the list after both images" "$r"

# The whole point: the prompt survives as the positional argument.
if [[ -n "$prompt_idx" && -n "$sep" && "$prompt_idx" -gt "$sep" ]]; then r=ok; else r=no; fi
check "prompt lands after -- (not swallowed)" "$r"

# The no-image path must keep working — that is Phase 2, which works today.
ARGV_NOIMG="$WORK/argv.noimg"
: >"$ARGV_NOIMG"
noimg_count="$WORK/count.noimg"
: >"$noimg_count"
PATH="$STUB_DIR:$PATH" CODEX_STUB_MODE=ok CODEX_STUB_COUNT="$noimg_count" \
  CODEX_STUB_ARGV="$ARGV_NOIMG" "$SUT" --prompt-file "$PROMPT" >/dev/null 2>&1
grep -qxF -- '--image' "$ARGV_NOIMG" && r=no || r=ok
check "no --image flag when no images given" "$r"
if grep -qxF -- '--' "$ARGV_NOIMG" && grep -qxF -- 'review this diff' "$ARGV_NOIMG"; then
  r=ok; else r=no; fi
check "prompt still delivered with no images" "$r"

echo
echo "passed $PASS, failed $FAIL"
(( FAIL == 0 ))
