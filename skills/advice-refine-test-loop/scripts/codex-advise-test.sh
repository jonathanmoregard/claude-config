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

emit_ok() { printf '%s\n' '{"verdict":"PASS","verdict_reason":"nothing found","findings":[]}'; }

case "${CODEX_STUB_MODE:-ok}" in
  ok)                emit_ok ;;
  quota)             echo "stream error: usage limit reached" >&2; exit 1 ;;
  quota_then_ok)     if (( n == 1 )); then echo "usage limit reached" >&2; exit 1; else emit_ok; fi ;;
  unparseable)       echo "Looks fine to me, no major issues." ;;
  unparseable_then_ok) if (( n == 1 )); then echo "Looks fine, SAFE-ish"; else emit_ok; fi ;;
  wrong_verdict)     printf '%s\n' '{"verdict":"LGTM","findings":[]}' ;;
  stall)             sleep 5 ;;
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
echo "passed $PASS, failed $FAIL"
(( FAIL == 0 ))
