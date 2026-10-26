#!/usr/bin/env bash
# statusline-quota-tee.sh — build contract §Hooks 1 (ai-router, 2026-09-14).
#
# Claude Code feeds the statusline command a JSON document on stdin; since
# v2.1.80 it carries `rate_limits` (five_hour / seven_day … with
# used_percentage + resets_at) once the first API response has arrived. This
# wrapper tees that key into $AI_ROUTER_STATE/quota/claude.json — the RAW
# snapshot the router's Claude quota probe reads — and renders a small local
# statusline. Tests can supply AI_ROUTER_STATUSLINE_CMD; that command receives
# the same stdin bytes and its stdout, stderr and exit code are preserved.
#
# Invariants:
#   * the tee half can never break the statusline (`|| true`, no `set -e`);
#   * the file is written only when `.rate_limits` is non-null; otherwise the
#     existing snapshot is left untouched;
#   * writes are atomic (mktemp in the target dir, then mv) and the state dir
#     is created 0700;
#   * added latency stays well under the 50 ms budget: jq is ~3 ms per call
#     (measured 2026-09-14); the python3 -S -E fallback (~35 ms) is used only
#     when jq is absent from PATH.
#
# Env:
#   AI_ROUTER_STATE           state dir (default ~/.local/state/ai-router)
#   AI_ROUTER_STATUSLINE_CMD  optional statusline command line (tests/custom)
set -u

# Capture stdin once; if that is impossible, run the statusline untouched.
tmp_in="$(mktemp "${TMPDIR:-/tmp}/statusline-stdin.XXXXXX" 2>/dev/null)" || {
  if [ -n "${AI_ROUTER_STATUSLINE_CMD:-}" ]; then
    eval "exec $AI_ROUTER_STATUSLINE_CMD" < /dev/null
  fi
  exit 0
}
cat > "$tmp_in" 2>/dev/null || true

tee_quota() {
  local state_dir="${AI_ROUTER_STATE:-$HOME/.local/state/ai-router}"
  local quota_dir="$state_dir/quota"
  local now="${EPOCHSECONDS:-$(date +%s)}"
  local out_tmp doc

  if command -v jq >/dev/null 2>&1; then
    doc="$(jq -c --argjson now "$now" '
      if (.rate_limits? // null) == null then empty else
        {provider: "claude", source: "statusline", observed_at: $now,
         cli_version: (.version? // null), rate_limits: .rate_limits}
      end' "$tmp_in" 2>/dev/null)" || return 0
  else
    doc="$(python3 -S -E - "$tmp_in" "$now" 2>/dev/null <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        data = json.load(fh)
except Exception:
    sys.exit(0)
if not isinstance(data, dict) or data.get("rate_limits") is None:
    sys.exit(0)
print(json.dumps({"provider": "claude", "source": "statusline",
                  "observed_at": int(sys.argv[2]),
                  "cli_version": data.get("version"),
                  "rate_limits": data["rate_limits"]}, separators=(",", ":")))
PY
)" || return 0
  fi
  [ -n "$doc" ] || return 0

  umask 077
  mkdir -p "$quota_dir" || return 0
  out_tmp="$(mktemp "$quota_dir/claude.json.tmp.XXXXXX")" || return 0
  if printf '%s\n' "$doc" > "$out_tmp"; then
    mv -f "$out_tmp" "$quota_dir/claude.json" || rm -f "$out_tmp"
  else
    rm -f "$out_tmp"
  fi
  return 0
}

( tee_quota ) 2>/dev/null || true

if [ -n "${AI_ROUTER_STATUSLINE_CMD:-}" ]; then
  # Hand the captured bytes to the custom statusline, unlink the temp file
  # first (the open fd keeps it readable), then replace this process.
  exec 3< "$tmp_in" || { rm -f "$tmp_in"; eval "exec $AI_ROUTER_STATUSLINE_CMD" < /dev/null; }
  rm -f "$tmp_in"
  exec 0<&3 3<&-
  eval "exec $AI_ROUTER_STATUSLINE_CMD"
fi

# Native passive renderer: local JSON parsing only, with no model/API calls.
if command -v jq >/dev/null 2>&1; then
  jq -r '
    def pct($label; $value):
      if $value == null then empty
      else (($value | round | tostring) + "% " + $label)
      end;
    def num: if type == "number" then . else 0 end;
    def cache:
      (.context_window.current_usage? // null) as $u
      | if ($u | type) != "object" then empty else
          ($u.cache_read_input_tokens | num) as $r
          | ($r + ($u.cache_creation_input_tokens | num) + ($u.input_tokens | num)) as $t
          | if $t > 0 then ("cache " + (($r * 100 / $t) | round | tostring) + "%") else empty end
        end;
    [
      (.model.display_name // .model.id // "Claude"),
      pct("ctx"; .context_window.used_percentage?),
      pct("5h"; .rate_limits.five_hour.used_percentage?),
      pct("week"; .rate_limits.seven_day.used_percentage?),
      cache
    ] | join(" · ")
  ' "$tmp_in" 2>/dev/null || true
else
  python3 -S -E - "$tmp_in" 2>/dev/null <<'PY' || true
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        data = json.load(fh)
except Exception:
    raise SystemExit(0)

model = data.get("model") or {}
parts = [model.get("display_name") or model.get("id") or "Claude"]
for label, path in (
    ("ctx", ("context_window", "used_percentage")),
    ("5h", ("rate_limits", "five_hour", "used_percentage")),
    ("week", ("rate_limits", "seven_day", "used_percentage")),
):
    value = data
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        parts.append(f"{round(value)}% {label}")
usage = (data.get("context_window") or {}).get("current_usage")
if isinstance(usage, dict):
    def num(k):
        v = usage.get(k)
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0
    total = num("cache_read_input_tokens") + num("cache_creation_input_tokens") + num("input_tokens")
    if total > 0:
        parts.append(f"cache {round(num('cache_read_input_tokens') * 100 / total)}%")
print(" · ".join(parts))
PY
fi
rm -f "$tmp_in"
