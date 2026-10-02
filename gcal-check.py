#!/usr/bin/env python3
"""
Check Google Calendar for events with a given person in the next N days.
Counts events where the partner's name appears (attendees, title, or description).
Prints DATE_COUNT and a list to stdout.
Exit 0 always (errors are printed but don't fail the caller).

Reads the calendar's private iCal feed ("Secret address in iCal format" in
Google Calendar settings) instead of the Calendar API: no OAuth client, no
refresh token to expire. The feed URL is a credential (anyone holding it can
read the calendar), so it lives in an agenix secret, not in this repo.

Needs the icalendar and recurring-ical-events packages; date-check.sh runs
this under a nix-provided python that has them.

Usage:
  python3 ~/.claude/gcal-check.py --partner "the partner" --days 31
"""
import argparse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ICAL_URL_PATH = Path("/run/agenix/gcal-ical-url")
FETCH_TIMEOUT_S = 30


def is_event_with_partner(event, partner_name):
    """Return True if partner's name appears in attendees, title, or description."""
    partner_lower = partner_name.lower()
    title = str(event.get("SUMMARY") or "").lower()
    description = str(event.get("DESCRIPTION") or "").lower()

    attendees = event.get("ATTENDEE") or []
    if not isinstance(attendees, list):
        attendees = [attendees]
    for att in attendees:
        name = str(att.params.get("CN") or "").lower()
        email = str(att).lower()
        if partner_lower in name or partner_lower in email:
            return True

    return partner_lower in title or partner_lower in description


def event_start(event):
    """Start as a date (all-day events) or datetime, for sorting and display."""
    return event["DTSTART"].dt


def sort_key(event):
    start = event_start(event)
    if isinstance(start, datetime):
        return start if start.tzinfo else start.replace(tzinfo=timezone.utc)
    return datetime(start.year, start.month, start.day, tzinfo=timezone.utc)


def normalize_url(url):
    """webcal:// is the subscribe-link alias of https:// for the same feed."""
    if url.lower().startswith("webcal://"):
        return "https://" + url[len("webcal://"):]
    return url


def url_problem(url):
    """Describe why url is not a fetchable https URL, without echoing it."""
    if any(c.isspace() or ord(c) < 0x20 for c in url):
        return "URL contains whitespace or control characters (wrapped paste or extra lines?)"
    if url[:1] in "\"'":
        return "URL is wrapped in quotes"
    scheme, sep, _ = url.partition("://")
    if not sep:
        return "URL has no scheme (expected https://...)"
    if scheme.lower() != "https":
        return f"URL scheme is not https ({len(scheme)} chars before ://)"
    return None


def fetch_error_summary(e):
    """Describe a fetch failure without any text that could contain the URL."""
    code = getattr(e, "code", None)
    if isinstance(code, int):
        return f"HTTP {code}"
    reason = getattr(e, "reason", None)
    if isinstance(reason, OSError):
        return f"{type(e).__name__}: {type(reason).__name__} {reason.strerror or ''}".rstrip()
    return f"{type(e).__name__}: {type(reason).__name__ if reason is not None else 'no detail'}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--partner", required=True)
    parser.add_argument("--days", type=int, default=31)
    args = parser.parse_args()

    try:
        url = ICAL_URL_PATH.read_text().strip()
    except OSError as e:
        print(f"GCAL_ERROR: cannot read iCal URL from {ICAL_URL_PATH}: {e.strerror}")
        return
    if not url:
        print(f"GCAL_ERROR: {ICAL_URL_PATH} is empty")
        return
    url = normalize_url(url)
    problem = url_problem(url)
    if problem:
        print(f"GCAL_ERROR: {ICAL_URL_PATH}: {problem}")
        return

    try:
        import icalendar
        import recurring_ical_events
    except ImportError as e:
        print(f"GCAL_ERROR: missing python package: {e.name}")
        return

    try:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_S) as resp:
            raw = resp.read()
    except Exception as e:
        # Never print the URL or anything that may echo it (exception text,
        # URLError.reason with a path): the URL is the credential.
        print(f"GCAL_ERROR: calendar fetch failed: {fetch_error_summary(e)}")
        return

    try:
        cal = icalendar.Calendar.from_ical(raw)
        today = date.today()
        # Expands recurring events. A single cancelled occurrence comes back
        # as an override with STATUS:CANCELLED, so filter those below.
        events = recurring_ical_events.of(cal).between(today, today + timedelta(days=args.days))
    except Exception as e:
        print(f"GCAL_ERROR: could not parse calendar: {type(e).__name__}: {e}")
        return

    date_events = sorted(
        (e for e in events
         if str(e.get("STATUS") or "").upper() != "CANCELLED"
         and is_event_with_partner(e, args.partner)),
        key=sort_key,
    )

    print(f"DATE_COUNT: {len(date_events)}")
    for e in date_events:
        when = event_start(e).isoformat()[:10]
        title = str(e.get("SUMMARY") or "(no title)")
        print(f"  - {when}: {title}")


if __name__ == "__main__":
    main()
