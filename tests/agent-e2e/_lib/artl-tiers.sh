#!/usr/bin/env bash
# Fixture for artl-tier-t1-one-reviewer, artl-delta-round and
# artl-round3-asks-redesign: a daily upload quota (plain Python, unittest) on
# feat/upload-quota. Usage: artl-tiers.sh WORK RUN t1|delta|round3
#   t1      the branch adds the daily reset to an existing quota: a T1 diff
#           (by the skill's tier table). Planted: the reset compares the day of the
#           month, in UTC, so 5 Jan's usage still counts on 5 Feb.
#   delta   a T2 diff, resumed after round 1: its findings are adjudicated in
#           review/round-1.md and the ledger, and the fix for the reproduced
#           one is committed on top of the tree round 1 reviewed.
#   round3  a T2 diff, resumed after round 3: rounds 1 and 2 each reproduced a
#           day-boundary defect in app/quota.py and were fixed; round 3
#           reproduced a third, in the line round 2's fix wrote (the offset
#           is cut to whole hours). Its reproducer is red in tests/test_round3.py.
# Writes RUN/start-commit. Nothing here hints at how many reviewers to use.
set -euo pipefail
WORK="${1:?work dir}" RUN="${2:?run dir}" MODE="${3:?t1|delta|round3}"
cd "$WORK"
git init -q -b main
git config user.email e2e@example.invalid
git config user.name e2e
commit() { git add -A && git commit -qm "$1"; }
line() { grep -n -m1 -- "$2" "$1" | cut -d: -f1; }
mkdir -p app tests scripts docs review
printf 'review/\n' >> .git/info/exclude
printf '__pycache__/\n' > .gitignore
: > app/__init__.py
printf '# filebox\n\nFile uploads with a daily quota. Gate: `scripts/check`.\n' > README.md
printf '#!/usr/bin/env bash\nset -euo pipefail\ncd "$(dirname "$0")/.."\npython3 -m unittest discover -s tests -q\n' > scripts/check
chmod +x scripts/check

cat > docs/quota.md <<'M'
# Upload quota acceptance

1. A user may upload at most 100 MiB per day.
2. The day is the user's local calendar day: usage resets at midnight at the
   user's UTC offset, in minutes (`Quota.set_offset`, default 0).
3. An upload over 25 MiB is refused with 413 and uses no quota.
4. An upload over the remaining quota is refused with 429 and uses no quota.
5. A file name that is empty, starts with a dot or contains "/" is refused
   with 400.
M

quota() { # DAY_EXPR: what _day returns, given user and ts (unix seconds)
  cat > app/quota.py <<P
"""Per-user daily upload quota (docs/quota.md)."""
from datetime import datetime, timedelta, timezone

DAILY_BYTES = 100 * 1024 * 1024


class QuotaExceeded(Exception):
    """The upload would take the user past today's quota."""


class Quota:
    def __init__(self, daily_bytes=DAILY_BYTES):
        self.daily_bytes = daily_bytes
        self.offsets = {}   # user -> UTC offset in minutes
        self.used = {}      # user -> bytes used on last_day
        self.last_day = {}  # user -> the day of the user's last charge

    def set_offset(self, user, minutes):
        self.offsets[user] = minutes

    def _day(self, user, ts):
        """The user's current day, for the daily reset."""
        return $1

    def _rollover(self, user, ts):
        day = self._day(user, ts)
        if self.last_day.get(user) != day:
            self.used[user] = 0
            self.last_day[user] = day

    def remaining(self, user, ts):
        self._rollover(user, ts)
        return self.daily_bytes - self.used[user]

    def charge(self, user, nbytes, ts):
        if nbytes > self.remaining(user, ts):
            raise QuotaExceeded(user)
        self.used[user] += nbytes
P
}
DOM='datetime.fromtimestamp(ts, timezone.utc).day'
UTC='datetime.fromtimestamp(ts, timezone.utc).date()'
LOCAL='datetime.fromtimestamp(ts, timezone(timedelta(minutes=self.offsets.get(user, 0)))).date()'
HOURS='datetime.fromtimestamp(ts, timezone(timedelta(hours=self.offsets.get(user, 0) // 60))).date()'

uploads() {
  cat > app/uploads.py <<'P'
"""Upload handler (docs/quota.md)."""
from app.quota import QuotaExceeded

MAX_UPLOAD_BYTES = 25 * 1024 * 1024


def accept(quota, store, user, name, data, ts):
    """Store an upload: 201, or 400 bad name, 413 too large, 429 over quota."""
    if not name or "/" in name or name.startswith("."):
        return 400, "bad file name"
    if len(data) > MAX_UPLOAD_BYTES:
        return 413, "file too large"
    try:
        quota.charge(user, len(data), ts)
    except QuotaExceeded:
        return 429, "daily quota used up"
    store[(user, name)] = data
    return 201, name


def listing(store, user):
    """The user's file names, sorted."""
    return sorted(name for (owner, name) in store if owner == user)
P
}

tests_head() {
  cat > tests/test_quota.py <<'P'
import unittest
from datetime import datetime, timezone

from app.quota import Quota, QuotaExceeded
from app.uploads import accept, listing

MiB = 1024 * 1024


def at(text):
    """Unix seconds for an ISO time in UTC."""
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()


class Quotas(unittest.TestCase):
    def setUp(self):
        self.quota, self.store = Quota(), {}

    def test_charge_reduces_remaining(self):
        self.quota.charge("u1", 10 * MiB, at("2026-01-05T09:00"))
        self.assertEqual(self.quota.remaining("u1", at("2026-01-05T10:00")), 90 * MiB)

    def test_over_quota_is_refused(self):
        self.quota.charge("u1", 95 * MiB, at("2026-01-05T09:00"))
        with self.assertRaises(QuotaExceeded):
            self.quota.charge("u1", 10 * MiB, at("2026-01-05T10:00"))
P
}
test_case() { printf '\n    def test_%s(self):\n%s\n' "$1" "$2" >> "${3:-tests/test_quota.py}"; }
NEXT_DAY='        self.quota.charge("u1", 95 * MiB, at("2026-01-05T09:00"))
        self.assertEqual(self.quota.remaining("u1", at("2026-01-06T09:00")), 100 * MiB)'
UPLOAD_TESTS() {
  test_case upload_is_stored '        self.assertEqual(accept(self.quota, self.store, "u1", "a.txt", b"hi", at("2026-01-05T09:00")), (201, "a.txt"))
        self.assertEqual(listing(self.store, "u1"), ["a.txt"])'
  test_case bad_name_is_400 '        self.assertEqual(accept(self.quota, self.store, "u1", ".env", b"x", at("2026-01-05T09:00"))[0], 400)'
  test_case too_large_is_413 '        self.assertEqual(accept(self.quota, self.store, "u1", "big", b"x" * (26 * MiB), at("2026-01-05T09:00"))[0], 413)'
}
NEXT_MONTH='        self.quota.charge("u1", 95 * MiB, at("2026-01-05T09:00"))
        self.assertEqual(self.quota.remaining("u1", at("2026-02-05T09:00")), 100 * MiB)'
LOCAL_MIDNIGHT='        self.quota.set_offset("u1", 600)  # UTC+10: local midnight is 14:00 UTC
        self.quota.charge("u1", 95 * MiB, at("2026-01-05T13:00"))
        self.assertEqual(self.quota.remaining("u1", at("2026-01-05T14:30")), 100 * MiB)'
HALF_HOUR='        self.quota.set_offset("u1", 330)  # UTC+05:30: local midnight is 18:30 UTC
        self.quota.charge("u1", 95 * MiB, at("2026-01-05T18:10"))
        self.assertEqual(self.quota.remaining("u1", at("2026-01-05T18:45")), 100 * MiB)'

notes="$HOME/.local/state/claude-tasks/$(basename "$WORK")"
mkdir -p "$notes"
ledger() { # ROW...: "run<TAB>check<TAB>where<TAB>claim<TAB>outcome<TAB>fixed<TAB>fp_reason"
  { printf 'date\tsource\trun\tcheck\ttrigger\twhere\tclaim\toutcome\tfixed\tfp_reason\n'
    for r in "$@"; do
      IFS=$'\t' read -r run check where claim outcome fixed reason <<<"$r"
      printf '2026-11-20\tartl\t%s\t%s\talways\t%s\t%s\t%s\t%s\t%s\n' \
        "$run" "$check" "$where" "$claim" "$outcome" "$fixed" "$reason"
    done; } > "$notes/review-ledger.tsv"
}

if [ "$MODE" = t1 ]; then
  # main: the quota with no reset yet, and the upload handler.
  cat > app/quota.py <<'P'
"""Per-user upload quota (docs/quota.md)."""

DAILY_BYTES = 100 * 1024 * 1024


class QuotaExceeded(Exception):
    """The upload would take the user past the quota."""


class Quota:
    def __init__(self, daily_bytes=DAILY_BYTES):
        self.daily_bytes = daily_bytes
        self.used = {}  # user -> bytes used

    def remaining(self, user, ts):
        return self.daily_bytes - self.used.get(user, 0)

    def charge(self, user, nbytes, ts):
        if nbytes > self.remaining(user, ts):
            raise QuotaExceeded(user)
        self.used[user] = self.used.get(user, 0) + nbytes
P
  uploads
  tests_head
  UPLOAD_TESTS
  commit "init: uploads with a per-user quota"
  git switch -qc feat/upload-quota
  quota "$DOM"
  test_case next_day_resets "$NEXT_DAY"
  commit "feat(quota): reset each user's quota daily"
  ./scripts/check
  git rev-parse HEAD > "$RUN/start-commit"
  exit 0
fi

# delta, round3: the whole quota is the branch's work.
printf 'import unittest\n\n\nclass Smoke(unittest.TestCase):\n    def test_package_imports(self):\n        import app  # noqa: F401\n' > tests/test_smoke.py
commit "init: filebox skeleton"
git switch -qc feat/upload-quota
quota "$DOM"
uploads
tests_head
test_case next_day_resets "$NEXT_DAY"
UPLOAD_TESTS
commit "feat(quota): daily upload quota and upload handler"
reviewed=$(git rev-parse --short HEAD)
tok="$reviewed-1030"
r1_where="app/quota.py:$(line app/quota.py 'def _day')"
r1_claim="_day compares only the day of the month, in UTC, so 5 Jan's usage still counts on 5 Feb and the reset ignores the user's offset; acceptance 2"
refuted_where="app/uploads.py:$(line app/uploads.py 'def accept')"
refuted_claim="accept charges the quota before the size check, so an upload refused with 413 still uses quota; acceptance 3"
refuted_why="app/uploads.py:$(line app/uploads.py 'return 413') returns before quota.charge"

if [ "$MODE" = delta ]; then
  quota "$LOCAL"
  test_case same_day_of_next_month_resets "$NEXT_MONTH"
  test_case resets_at_local_midnight "$LOCAL_MIDNIGHT"
  commit "fix(quota): reset on the user's local calendar date"
  fix=$(git rev-parse --short HEAD)
  ledger "$tok/r1	A	$r1_where	$r1_claim	reproduced	yes	-" \
         "$tok/r1	D1	$refuted_where	$refuted_claim	refuted	-	returns before the charge"
  cat > review/round-1.md <<M
# Close-out review of feat/upload-quota: round 1

Tier T2. Tree reviewed: $reviewed (main...$reviewed), close-out token $tok.
Acceptance criteria: docs/quota.md. Two blind reviewers ran and the verifier
tested every defect finding; the rows are in the review ledger.

| # | check | where | claim | outcome | note |
|---|---|---|---|---|---|
| 1 | A | $r1_where | $r1_claim | reproduced | fixed in $fix (fix(quota): reset on the user's local calendar date); reproducers test_same_day_of_next_month_resets, test_resets_at_local_midnight |
| 2 | D1 | $refuted_where | $refuted_claim | refuted | $refuted_why; the reproducer passed |

The gate (scripts/check) is green after the fix. Nothing has been edited since.
M
  ./scripts/check
  git rev-parse HEAD > "$RUN/start-commit"
  exit 0
fi

# round3
quota "$UTC"
test_case same_day_of_next_month_resets "$NEXT_MONTH"
commit "fix(quota): reset on the calendar date, not the day of the month"
r1_fix=$(git rev-parse --short HEAD)
r2_where="app/quota.py:$(line app/quota.py 'def _day')"
r2_claim="_day takes the UTC date, so a UTC+10 user's quota resets at 10:00 local, not at local midnight; acceptance 2"
quota "$HOURS"
test_case resets_at_local_midnight "$LOCAL_MIDNIGHT"
commit "fix(quota): reset at the user's local midnight"
r2_fix=$(git rev-parse --short HEAD)
r3_where="app/quota.py:$(line app/quota.py 'timedelta(hours=')"
printf '"""Round 3 reproducer, red until fixed."""\nimport unittest\n\nfrom app.quota import Quota\nfrom test_quota import MiB, at\n\n\nclass Round3(unittest.TestCase):\n    def setUp(self):\n        self.quota = Quota()\n' > tests/test_round3.py
test_case resets_at_half_hour_local_midnight "$HALF_HOUR" tests/test_round3.py
commit "test: round 3 reproducer"
ledger "$tok/r1	A	$r1_where	$r1_claim	reproduced	yes	-" \
       "$tok/r1	D1	$refuted_where	$refuted_claim	refuted	-	returns before the charge" \
       "$tok/r2	A	$r2_where	$r2_claim	reproduced	yes	-"
cat > review/round-3.md <<M
# Close-out review of feat/upload-quota: round 3

Tier T2, close-out token $tok. Acceptance criteria: docs/quota.md.
Round 1 (tree $reviewed) and round 2 (delta $r1_fix) are in the review ledger;
each reproduced one finding in app/quota.py, fixed in $r1_fix and $r2_fix.
Round 3 reviewed the delta $r2_fix, and the verifier tested its finding; the
reproducer is tests/test_round3.py, red until fixed. Nothing has been edited
since.

| # | check | where | claim | outcome | origin |
|---|---|---|---|---|---|
| 1 | A | $r3_where | _day builds the offset with timedelta(hours=offset // 60), dropping the minutes, so a UTC+05:30 user's quota resets at 23:30 local, not at midnight; acceptance 2 | reproduced | the round 2 fix ($r2_fix) |
M
if ./scripts/check 2>/dev/null; then echo "fixture fault: the round 3 reproducer passes" >&2; exit 1; fi
git rev-parse HEAD > "$RUN/start-commit"
