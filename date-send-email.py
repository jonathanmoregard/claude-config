#!/usr/bin/env python3
"""
Sends the date-night reminder email via Gmail SMTP.
Credentials via environment variables (source ~/.config/date-check/env):
  DATE_CHECK_SMTP_USER         your Gmail address
  DATE_CHECK_SMTP_APP_PASSWORD app password from myaccount.google.com/apppasswords

Usage: python3 date-send-email.py "<subject>" "<body>"
"""
import os
import smtplib
import sys
from email.mime.text import MIMEText


def main():
    if len(sys.argv) < 3:
        print("Usage: date-send-email.py <subject> <body>", file=sys.stderr)
        sys.exit(1)

    subject = sys.argv[1]
    body = sys.argv[2]

    user = os.environ.get("DATE_CHECK_SMTP_USER", "")
    password = os.environ.get("DATE_CHECK_SMTP_APP_PASSWORD", "")

    if not user or not password:
        print("Missing DATE_CHECK_SMTP_USER or DATE_CHECK_SMTP_APP_PASSWORD env vars", file=sys.stderr)
        print("Set them in ~/.config/date-check/env", file=sys.stderr)
        sys.exit(1)

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = user  # send to yourself

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(user, password)
        server.send_message(msg)

    print(f"Email sent to {user}")


if __name__ == "__main__":
    main()
