#!/usr/bin/env bash
# Deterministic rules gate for advice-refine-test-loop.
#
# Every other phase of the loop is an LLM forming a judgement. An LLM judge can
# be argued out of a verdict — by a persuasive diff comment, by a plausible
# rationalisation in its own reasoning, or by a later phase's framing. Regex
# cannot. That asymmetry is the entire point of this script: it is the layer
# whose verdict no downstream phase is permitted to overturn.
#
# It is deliberately narrow. It only reports things that are true by pattern
# match, never things that require understanding intent. Anything needing
# judgement belongs in Phase 1-3, not here — a rules layer that tries to be
# clever inherits the false-positive rate that makes people start ignoring it.
#
# Usage:
#   deterministic-gate.sh [--range <git-range>] [--json] [--repo <path>]
#
#   --range   Diff range to scan. Default: origin/<default-branch>...HEAD,
#             falling back to staged+unstaged working tree changes.
#   --json    Emit JSON (for the loop). Default is human-readable text.
#
# Exit: 0 = clean, 1 = findings, 2 = the gate itself could not run.
#
# Exit 2 matters: "the scanner did not run" must never be indistinguishable
# from "the scanner found nothing". A gate that fails open is not a gate.
#
# Waivers: a match is suppressed when the matched line, or the line directly
# above it, contains `arftl-allow: <rule-id>`. Waivers live in the diff, so
# they are reviewable, greppable, and attributable to a commit. There is no
# config file and no global suppression, because both are the mechanisms by
# which a rules layer quietly stops covering anything.

set -uo pipefail

RANGE=""
JSON=0
REPO="."
INCLUDE_DOCS=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --range) RANGE="${2:-}"; shift 2 ;;
    --json)  JSON=1; shift ;;
    --repo)  REPO="${2:-}"; shift 2 ;;
    --include-docs) INCLUDE_DOCS=1; shift ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

cd "$REPO" 2>/dev/null || { echo "gate: cannot cd to $REPO" >&2; exit 2; }
git rev-parse --git-dir >/dev/null 2>&1 || { echo "gate: not a git repo: $REPO" >&2; exit 2; }

# ── Resolve the range ────────────────────────────────────────────────────────
if [[ -z "$RANGE" ]]; then
  default_branch=""
  if head_ref=$(git symbolic-ref --quiet refs/remotes/origin/HEAD 2>/dev/null); then
    default_branch="${head_ref#refs/remotes/origin/}"
  fi
  for cand in "$default_branch" main master; do
    [[ -n "$cand" ]] || continue
    if git rev-parse --verify --quiet "origin/$cand" >/dev/null; then
      RANGE="origin/$cand...HEAD"
      break
    fi
  done
fi

if [[ -n "$RANGE" ]]; then
  DIFF=$(git diff --unified=0 "$RANGE" 2>/dev/null)
  diff_rc=$?
  if [[ $diff_rc -ne 0 ]]; then
    echo "gate: could not diff range '$RANGE'" >&2
    exit 2
  fi
else
  # No upstream to compare against — fall back to the working tree so the gate
  # still covers something rather than silently passing.
  DIFF=$(git diff --unified=0 HEAD 2>/dev/null)
  RANGE="(working tree vs HEAD)"
fi

# ── Rules ────────────────────────────────────────────────────────────────────
# Each rule is `id<TAB>severity<TAB>ERE`. Matched against ADDED lines only:
# this gate judges what the diff introduces, not what it inherited. Removing a
# pre-existing offender must never be reported as introducing one.
#
# Kept to patterns whose match IS the finding. No heuristics, no "looks like".
read -r -d '' RULES <<'EOF'
destructive-rm	BLOCKER	rm[[:space:]]+(-[[:alnum:]-]+[[:space:]]+)*-[[:alnum:]]*[rR][[:alnum:]]*[[:space:]]+(-[[:alnum:]-]+[[:space:]]+)*("?(/[A-Za-z]|~|\$HOME|\$\{HOME)|\$[A-Za-z_][A-Za-z0-9_]*|\*)
force-push	BLOCKER	git[[:space:]]+push[[:space:]].*(--force([[:space:]]|$)|-f([[:space:]]|$))
history-rewrite	BLOCKER	git[[:space:]]+(reset[[:space:]]+--hard|filter-branch|clean[[:space:]]+-[a-z]*f)
sql-drop	BLOCKER	(DROP[[:space:]]+(TABLE|DATABASE|SCHEMA)|TRUNCATE[[:space:]]+TABLE)
sql-unscoped-delete	BLOCKER	(DELETE[[:space:]]+FROM|UPDATE)[[:space:]]+[A-Za-z_][A-Za-z0-9_.]*[[:space:]]*(;|$)
curl-pipe-shell	BLOCKER	(curl|wget)[^|]*\|[[:space:]]*(sudo[[:space:]]+)?(ba)?sh
tls-verify-off	BLOCKER	(verify[[:space:]]*=[[:space:]]*False|sslVerify[[:space:]]+false|--insecure|-k[[:space:]]+https|NODE_TLS_REJECT_UNAUTHORIZED[[:space:]]*=[[:space:]]*.?0|InsecureSkipVerify[[:space:]]*:[[:space:]]*true|CURLOPT_SSL_VERIFYPEER[[:space:]]*,[[:space:]]*(false|0))
host-key-off	BLOCKER	StrictHostKeyChecking[[:space:]=]+no
world-writable	HIGH	chmod[[:space:]]+(-[a-zA-Z]+[[:space:]]+)*(777|a\+w|o\+w)
swallowed-exception	HIGH	(except[[:space:]]*[A-Za-z_.(),[:space:]]*:[[:space:]]*pass[[:space:]]*$|catch[[:space:]]*\([^)]*\)[[:space:]]*\{[[:space:]]*\})
verification-forced-green	HIGH	((test|pytest|jest|vitest|npm[[:space:]]+(run[[:space:]]+)?(test|lint|build|typecheck|check)|yarn[[:space:]]+(test|lint|build)|pnpm[[:space:]]+(test|lint|build)|make[[:space:]]+(test|check|lint)|cargo[[:space:]]+(test|clippy|build)|go[[:space:]]+(test|vet|build)|tsc|mypy|ruff|flake8|eslint|shellcheck|nix[[:space:]]+(build|flake[[:space:]]+check)|gitleaks|verify|lint|typecheck)[^|&]*\|\|[[:space:]]*true[[:space:]]*(#.*)?$|--exit-zero|continue-on-error:[[:space:]]*true)
suppression-marker	HIGH	(#[[:space:]]*nosec|#[[:space:]]*type:[[:space:]]*ignore|eslint-disable(-next-line)?|@ts-(ignore|nocheck)|#[[:space:]]*noqa[[:space:]]*$|nolint)
permission-widened	HIGH	("permissions"[[:space:]]*:|bypassPermissions|dangerously-skip-permissions|--dangerously)
EOF

# ── Scan ─────────────────────────────────────────────────────────────────────
# `git diff --unified=0` gives us hunk headers with the new-file line numbers,
# so added lines can be attributed to a real file:line rather than a diff
# offset. Without that, findings are unactionable.
FINDINGS=$(printf '%s\n' "$DIFF" | RULES="$RULES" awk -v include_docs="$INCLUDE_DOCS" '
function emit(rule, sev, file, lineno, text) {
  gsub(/\t/, " ", text)
  printf "%s\t%s\t%s\t%d\t%s\n", rule, sev, file, lineno, text
}
BEGIN {
  n = split(ENVIRON["RULES"], lines, "\n")
  nr = 0
  for (i = 1; i <= n; i++) {
    if (lines[i] == "") continue
    split(lines[i], f, "\t")
    nr++
    rid[nr] = f[1]; rsev[nr] = f[2]; rpat[nr] = f[3]
  }
}
/^\+\+\+ / {
  file = substr($0, 7)          # strip "+++ b/"
  if (file == "/dev/null") file = ""
  # This file states several rules as literal text (`eslint-disable`,
  # `bypassPermissions`, `--dangerously`), so scanning it makes the gate flag
  # its own rule table on every edit — and a gate that cries wolf about itself
  # is one people learn to skip. Excluded deliberately. The trade is a named,
  # single-file blind spot, which is why changes here get human review rather
  # than machine review.
  skip_file = (file ~ /deterministic-gate\.sh$/)
  # Prose files describe commands; they do not run them. A plan document that
  # says "then run git push --force" is a sentence, and flagging it buries the
  # diff lines that are actually executable. Measured against the history of
  # this repo, docs produced the overwhelming majority of all matches and none
  # of the true ones. gitleaks still covers these files, because a secret in a
  # document is still a secret.
  if (!skip_file && !include_docs)
    skip_file = (file ~ /\.(md|markdown|txt|rst|adoc)$/)
  next
}
/^@@ / {
  # @@ -a,b +c,d @@ — take c as the first new-file line of the hunk.
  match($0, /\+[0-9]+/)
  newline = substr($0, RSTART + 1, RLENGTH - 1) + 0
  next
}
/^\+/ {
  if (skip_file) next
  text = substr($0, 2)
  # Keep the previous added line so a waiver can sit directly above the match.
  for (i = 1; i <= nr; i++) {
    if (text ~ rpat[i]) {
      tag = "arftl-allow:[ ]*" rid[i]
      if (text ~ tag || prevtext ~ tag) { waived++ ; continue }
      emit(rid[i], rsev[i], file, newline, text)
    }
  }

  # Two-line swallowed-exception. The single-line `except X: pass` form is rare
  # in real code; the common shape spans two lines and a line-at-a-time regex
  # cannot see it. Only pair when the previous ADDED line is physically the
  # line above (prevline == newline - 1), so two added lines from different
  # hunks are never joined into a finding that does not exist.
  # Only the genuinely empty handler: bare `pass` or an immediately-closing
  # brace. `return None` inside an except block is a normal, deliberate way to
  # signal absence in a lookup helper — flagging it produced far more noise
  # than signal against a real corpus, and noise is what gets a rules layer
  # ignored.
  if (prevline == newline - 1 && text ~ /^[[:space:]]*(pass|\})[[:space:]]*$/) {
    if (prevtext ~ /^[[:space:]]*(except[[:space:]A-Za-z_.(),]*:|catch[[:space:]]*\([^)]*\)[[:space:]]*\{|rescue([[:space:]]|$))[[:space:]]*$/) {
      tag = "arftl-allow:[ ]*swallowed-exception"
      if (text ~ tag || prevtext ~ tag) { waived++ }
      else emit("swallowed-exception", "HIGH", file, newline, prevtext " / " text)
    }
  }

  prevtext = text
  prevline = newline
  newline++
  next
}
{ next }
END { if (waived > 0) printf "__WAIVED__\t%d\n", waived }
')

WAIVED=$(printf '%s\n' "$FINDINGS" | awk -F'\t' '$1=="__WAIVED__"{print $2}')
WAIVED=${WAIVED:-0}
FINDINGS=$(printf '%s\n' "$FINDINGS" | grep -v '^__WAIVED__' | sed '/^$/d')

# ── gitleaks ─────────────────────────────────────────────────────────────────
# Content-based secret detection, complementing the filename-shaped coverage in
# the global gitignore. Absence of gitleaks is exit 2, not a silent pass.
LEAKS=0
LEAK_DETAIL=""
if command -v gitleaks >/dev/null 2>&1; then
  leak_out=$(gitleaks git --staged --redact --no-banner 2>&1)
  if [[ $? -ne 0 ]]; then
    LEAKS=1
    LEAK_DETAIL=$(printf '%s' "$leak_out" | head -40)
  fi
else
  echo "gate: gitleaks not on PATH — cannot run the secret rules" >&2
  exit 2
fi

# ── Report ───────────────────────────────────────────────────────────────────
count=0
[[ -n "$FINDINGS" ]] && count=$(printf '%s\n' "$FINDINGS" | wc -l | tr -d ' ')

if [[ $JSON -eq 1 ]]; then
  printf '{"range":%s,"waived":%d,"gitleaks_clean":%s,"findings":[' \
    "$(printf '%s' "$RANGE" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')" \
    "$WAIVED" \
    "$([[ $LEAKS -eq 0 ]] && echo true || echo false)"
  first=1
  if [[ -n "$FINDINGS" ]]; then
    while IFS=$'\t' read -r rule sev file lineno text; do
      [[ -n "$rule" ]] || continue
      [[ $first -eq 1 ]] || printf ','
      first=0
      printf '{"rule":"%s","severity":"%s","file":%s,"line":%s,"text":%s}' \
        "$rule" "$sev" \
        "$(printf '%s' "$file" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')" \
        "$lineno" \
        "$(printf '%s' "$text" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')"
    done <<< "$FINDINGS"
  fi
  printf ']}\n'
else
  echo "deterministic gate — range: $RANGE"
  if [[ -n "$FINDINGS" ]]; then
    while IFS=$'\t' read -r rule sev file lineno text; do
      [[ -n "$rule" ]] || continue
      printf '  %-8s %-26s %s:%s\n      %s\n' "$sev" "$rule" "$file" "$lineno" "$text"
    done <<< "$FINDINGS"
  fi
  [[ $LEAKS -eq 1 ]] && { echo "  BLOCKER  gitleaks"; printf '%s\n' "$LEAK_DETAIL" | sed 's/^/      /'; }
  [[ $WAIVED -gt 0 ]] && echo "  ($WAIVED match(es) waived inline)"
  if [[ $count -eq 0 && $LEAKS -eq 0 ]]; then
    echo "  clean"
  fi
fi

[[ $count -eq 0 && $LEAKS -eq 0 ]] && exit 0
exit 1
