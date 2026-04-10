#!/usr/bin/env python3
"""
One-time Google Calendar OAuth setup.
Run this interactively once to store a refresh token.

Usage:
  python3 ~/.claude/gcal-auth.py --credentials /path/to/client_secret.json

Credentials file: download from Google Cloud Console:
  APIs & Services → Credentials → OAuth 2.0 Client ID (Desktop app) → Download JSON
  Store it at: ~/.config/gcal/client_secret.json

Token is saved to: ~/.config/gcal/token.json
"""
import argparse
import json
import os
import sys
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
TOKEN_PATH = Path.home() / ".config" / "gcal" / "token.json"
DEFAULT_CREDS = Path.home() / ".config" / "gcal" / "client_secret.json"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--credentials", default=str(DEFAULT_CREDS))
    args = parser.parse_args()

    creds_path = Path(args.credentials)
    if not creds_path.exists():
        print(f"Error: credentials file not found: {creds_path}")
        print()
        print("To create one:")
        print("  1. Go to https://console.cloud.google.com/")
        print("  2. Create a project (or select existing)")
        print("  3. Enable Google Calendar API")
        print("  4. APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client ID")
        print("  5. Application type: Desktop app")
        print("  6. Download JSON → save to:", DEFAULT_CREDS)
        sys.exit(1)

    from google_auth_oauthlib.flow import InstalledAppFlow

    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)

    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=False)

    TOKEN_PATH.write_text(json.dumps({
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes),
    }))
    os.chmod(TOKEN_PATH, 0o600)
    print(f"Token saved to {TOKEN_PATH}")


if __name__ == "__main__":
    main()
