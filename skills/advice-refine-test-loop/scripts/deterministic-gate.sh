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
#
# One structural exemption exists alongside waivers: a JSON string under
# `permissions.deny` or `permissions.ask` is data, not a command. See the
# "Permission tables are data" block below for why it is not a waiver.

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
    -h|--help) sed -n '2,34p' "$0"; exit 0 ;;
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

# Right-hand side of the range — the post-image whose structure a finding's
# line number addresses. Empty means the working tree, which is what the
# fallback above diffs against.
RANGE_TIP=""
[[ "$RANGE" == *".."* ]] && RANGE_TIP="${RANGE##*..}"

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
  # The gate and its fixture both state rules as literal text — the fixture
  # exists precisely to contain one example of every pattern — so scanning
  # either makes the gate flag its own rule table on every edit, and a gate
  # that cries wolf about itself is one people learn to skip.
  skip_file = (file ~ /deterministic-gate(-test)?\.sh$/)
  # Same reset as the hunk header, for the same reason: nothing in the previous
  # FILE is the line above anything in this one.
  prevtext = ""; prevline = -1
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
  # A new hunk means the previous added line is no longer ABOVE anything here.
  # Without this reset the waiver on the last added line of the previous hunk
  # reached the first added line of this one, silently suppressing a BLOCKER
  # across a gap of any size. See the waiver-scope lane in the test fixture.
  prevtext = ""; prevline = -1
  next
}
/^\+/ {
  if (skip_file) next
  text = substr($0, 2)
  # Keep the previous added line so a waiver can sit directly above the match.
  #
  # The `prevline == newline - 1` guard is what confines "directly above" to
  # the line PHYSICALLY above. Without it the previous added line waived a
  # match at any distance — across a gap inside one hunk, across hunks, and
  # across files — which is silent BLOCKER suppression, the one failure mode a
  # rules layer must not have. Same guard, same reason, as the
  # swallowed-exception pairing below; that one had it from the start and this
  # one did not.
  for (i = 1; i <= nr; i++) {
    if (text ~ rpat[i]) {
      tag = "arftl-allow:[ ]*" rid[i]
      if (text ~ tag || (prevline == newline - 1 && prevtext ~ tag)) { waived++ ; continue }
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

# ── Permission tables are data, not commands ─────────────────────────────────
# A string under `permissions.deny` or `permissions.ask` NAMES a command in
# order to forbid or gate it. Matching one is the rule working, not a hazard
# introduced. This is the same reason the scan already exempts this file and
# its fixture: a rule table that states hazards as literal text will flag
# itself on every edit, and a gate that cries wolf about itself is one people
# learn to skip. A permissions block is another rule table.
#
# It is an exemption rather than a waiver because the waiver mechanism cannot
# reach here: `arftl-allow:` must appear in a comment, and JSON has no comment
# syntax. Measured, and worth stating because the workaround is worse than the
# problem — with a `//` comment in a settings file the CLI silently voids the
# WHOLE permissions block, so waiving this way would disable every deny rule in
# it.
#
# Scoped by JSON path, never by filename. A string under `permissions.allow` is
# NOT exempt: that one auto-approves the command it names, which is the whole
# hazard. Anything the resolver cannot fetch or parse keeps its finding — the
# exemption fails closed.
read -r -d '' PERMISSION_DATA_PY <<'PY'
import json, os, subprocess, sys

TIP = os.environ.get("RANGE_TIP", "")
DATA_PATHS = (("permissions", "deny"), ("permissions", "ask"))
_src_cache = {}
_line_cache = {}


def source(path):
    """Post-image text of `path`, or None when it cannot be resolved."""
    if path in _src_cache:
        return _src_cache[path]
    text = None
    if TIP:
        p = subprocess.run(["git", "show", "%s:%s" % (TIP, path)],
                           capture_output=True, text=True)
        if p.returncode == 0:
            text = p.stdout
    if text is None:
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError):
            text = None
    _src_cache[path] = text
    return text


def data_lines(path):
    """Line numbers occupied by strings under a DATA_PATHS array.

    Hand-rolled rather than json.load, because a parsed object carries no line
    numbers and findings are addressed by line. Unresolvable or unparseable
    input yields the empty set, so the finding stands.
    """
    src = source(path)
    if src is None:
        return set()
    try:
        json.loads(src)
    except ValueError:
        return set()
    exempt, stack, i, n = set(), [], 0, len(src)
    while i < n:
        c = src[i]
        if c == '"':
            j = i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == '"':
                    break
                j += 1
            k = j + 1
            while k < n and src[k] in " \t\r\n":
                k += 1
            if k < n and src[k] == ":":
                # A key names the slot we are about to descend into.
                if stack and stack[-1][0] == "{":
                    stack[-1][1] = json.loads(src[i:j + 1])
            elif stack and stack[-1][0] == "[":
                keys = tuple(f[1] for f in stack if f[0] == "{")
                if keys in DATA_PATHS:
                    exempt.update(range(src.count("\n", 0, i) + 1,
                                        src.count("\n", 0, j) + 2))
            i = j + 1
            continue
        if c in "{[":
            stack.append([c, None])
        elif c in "}]" and stack:
            stack.pop()
        i += 1
    return exempt


dropped = 0
for line in sys.stdin:
    line = line.rstrip("\n")
    if not line:
        continue
    parts = line.split("\t")
    if len(parts) < 5 or not parts[2].endswith(".json"):
        print(line)
        continue
    f = parts[2]
    if f not in _line_cache:
        _line_cache[f] = data_lines(f)
    if int(parts[3]) in _line_cache[f]:
        dropped += 1
    else:
        print(line)
if dropped:
    print("__DATA_EXEMPT__\t%d" % dropped)
PY

DATA_EXEMPT=0
if [[ -n "$FINDINGS" ]]; then
  FINDINGS=$(printf '%s\n' "$FINDINGS" | RANGE_TIP="$RANGE_TIP" python3 -c "$PERMISSION_DATA_PY")
  if [[ $? -ne 0 ]]; then
    echo "gate: the permission-data resolver failed — refusing to report" >&2
    exit 2
  fi
  DATA_EXEMPT=$(printf '%s\n' "$FINDINGS" | awk -F'\t' '$1=="__DATA_EXEMPT__"{print $2}')
  DATA_EXEMPT=${DATA_EXEMPT:-0}
  FINDINGS=$(printf '%s\n' "$FINDINGS" | grep -v '^__DATA_EXEMPT__' | sed '/^$/d')
fi

# ── gitleaks ─────────────────────────────────────────────────────────────────
# Content-based secret detection, complementing the filename-shaped coverage in
# the global gitignore. Absence of gitleaks is exit 2, not a silent pass.
#
# IT MUST SCAN THE RANGE THE REST OF THE GATE JUDGES. This lane used to run
# `--staged` only, which reads the INDEX — while every pattern rule above reads
# the COMMIT RANGE. The two therefore never saw the same bytes, and the loop
# that calls this gate commits at every round's step 7, so from the second
# round on the index was empty: the lane scanned nothing, found nothing, and
# the gate reported `gitleaks_clean: true` over a committed secret sitting in
# the very range it had just pattern-scanned. Measured, both arms, in
# deterministic-gate-test.sh.
#
# Both are scanned now — the range for what is committed, the index for what is
# staged and not yet committed. A hit in either is a BLOCKER.
LEAKS=0
LEAK_DETAIL=""
if command -v gitleaks >/dev/null 2>&1; then
  leak_out=""
  # `git diff A...B` compares B against the merge base of A and B; the
  # commit-range spelling of that same set is `A..B`, which is what
  # `--log-opts` takes. A range with no `..` at all (a bare ref) leaves this
  # empty and only the index is scanned — stated so the gap is named rather
  # than discovered.
  if [[ "$RANGE" == *".."* ]]; then
    range_out=$(gitleaks git --redact --no-banner --log-opts="${RANGE/.../..}" 2>&1)
    if [[ $? -ne 0 ]]; then
      LEAKS=1
      leak_out="$range_out"
    fi
  fi
  staged_out=$(gitleaks git --staged --redact --no-banner 2>&1)
  if [[ $? -ne 0 ]]; then
    LEAKS=1
    leak_out="${leak_out}${leak_out:+$'\n'}$staged_out"
  fi
  [[ $LEAKS -eq 1 ]] && LEAK_DETAIL=$(printf '%s' "$leak_out" | head -40)
else
  echo "gate: gitleaks not on PATH — cannot run the secret rules" >&2
  exit 2
fi

# ── Report ───────────────────────────────────────────────────────────────────
count=0
[[ -n "$FINDINGS" ]] && count=$(printf '%s\n' "$FINDINGS" | wc -l | tr -d ' ')

if [[ $JSON -eq 1 ]]; then
  printf '{"range":%s,"waived":%d,"data_exempt":%d,"gitleaks_clean":%s,"findings":[' \
    "$(printf '%s' "$RANGE" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')" \
    "$WAIVED" \
    "$DATA_EXEMPT" \
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
  [[ $DATA_EXEMPT -gt 0 ]] && echo "  ($DATA_EXEMPT permission-rule string(s) exempt: data, not commands)"
  if [[ $count -eq 0 && $LEAKS -eq 0 ]]; then
    echo "  clean"
  fi
fi

[[ $count -eq 0 && $LEAKS -eq 0 ]] && exit 0
exit 1
