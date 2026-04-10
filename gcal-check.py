#!/usr/bin/env python3
"""
Check Google Calendar for events with a given person in the next N days.
Counts events where the partner's name appears (attendees, title, or description).
Prints DATE_COUNT and a list to stdout.
Exit 0 always (errors are printed but don't fail the caller).

Usage:
  python3 ~/.claude/gcal-check.py --partner "the partner" --days 31
"""
import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

TOKEN_PATH = Path.home() / ".config" / "gcal" / "token.json"
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


def load_creds():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    if not TOKEN_PATH.exists():
        return None

    data = json.loads(TOKEN_PATH.read_text())
    creds = Credentials(
        token=data["token"],
        refresh_token=data["refresh_token"],
        token_uri=data["token_uri"],
        client_id=data["client_id"],
        client_secret=data["client_secret"],
        scopes=data["scopes"],
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        data["token"] = creds.token
        TOKEN_PATH.write_text(json.dumps(data))
    return creds


def is_event_with_partner(event, partner_name):
    """Return True if partner's name appears in attendees, title, or description."""
    partner_lower = partner_name.lower()
    title = (event.get("summary") or "").lower()
    description = (event.get("description") or "").lower()

    for att in event.get("attendees", []):
        name = (att.get("displayName") or "").lower()
        email = (att.get("email") or "").lower()
        if partner_lower in name or partner_lower in email:
            return True

    return partner_lower in title or partner_lower in description


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--partner", required=True)
    parser.add_argument("--days", type=int, default=31)
    args = parser.parse_args()

    try:
        creds = load_creds()
    except Exception as e:
        print(f"GCAL_ERROR: could not load credentials: {e}")
        return

    if creds is None:
        print("GCAL_ERROR: no token found — run gcal-auth.py first")
        return

    try:
        from googleapiclient.discovery import build
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    except Exception as e:
        print(f"GCAL_ERROR: could not connect to Calendar API: {e}")
        return

    now = datetime.now(timezone.utc)
    end = now + timedelta(days=args.days)

    try:
        result = service.events().list(
            calendarId="primary",
            timeMin=now.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=100,
        ).execute()
    except Exception as e:
        print(f"GCAL_ERROR: calendar fetch failed: {e}")
        return

    events = result.get("items", [])
    date_events = [e for e in events if is_event_with_partner(e, args.partner)]

    print(f"DATE_COUNT: {len(date_events)}")
    for e in date_events:
        start = e.get("start", {})
        when = (start.get("dateTime") or start.get("date") or "?")[:10]
        title = e.get("summary") or "(no title)"
        print(f"  - {when}: {title}")


if __name__ == "__main__":
    main()
