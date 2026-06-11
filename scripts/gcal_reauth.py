#!/usr/bin/env python3
"""
Authorize Google Calendar for the cloud scheduler — one command.

Sibling of gmail_reauth.py. Calendar uses the same installed-app
OAuth client as Gmail but its own token (different scopes). Symptom
that you need this: scheduler logs show
    [calendar] token missing/invalid and no tty for the consent flow
and /calendar pages stay empty.

What it does:
  1. Pulls the OAuth client (GMAIL_CREDENTIALS_JSON) from the Railway
     email service.
  2. Runs the local-server consent flow for calendar scopes.
  3. Writes the token to the Railway SCHEDULER service as
     CALENDAR_TOKEN_JSON (+ sets CALENDAR_TOKEN_PATH/
     CALENDAR_CREDENTIALS_PATH to /tmp targets) and redeploys.

Usage:
    python3 scripts/gcal_reauth.py
Requires: railway CLI logged in; pip install google-auth-oauthlib
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

# MUST match astra/calendar/client.py SCOPES exactly.
SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]
CREDS_SOURCE_SERVICE = "email"
TARGET_SERVICE = "scheduler"
REPO_ROOT = Path(__file__).resolve().parents[1]


def _railway_get(service: str, var: str) -> str:
    out = subprocess.run(
        ["railway", "variables", "--service", service, "--kv"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=True,
    ).stdout
    for line in out.splitlines():
        if line.startswith(f"{var}="):
            return line.split("=", 1)[1]
    return ""


def _railway_set(service: str, **vars_: str) -> None:
    cmd = ["railway", "variables", "--service", service]
    for k, v in vars_.items():
        cmd += ["--set", f"{k}={v}"]
    subprocess.run(cmd, cwd=REPO_ROOT, check=True)


def main() -> int:
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("pip install google-auth-oauthlib — then rerun", file=sys.stderr)
        return 2

    creds_json = _railway_get(CREDS_SOURCE_SERVICE, "GMAIL_CREDENTIALS_JSON")
    if not creds_json:
        print(
            "GMAIL_CREDENTIALS_JSON not found on the Railway email service.",
            file=sys.stderr,
        )
        return 1

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        f.write(creds_json)
        client_path = f.name

    print("Opening browser for Google Calendar consent…")
    flow = InstalledAppFlow.from_client_secrets_file(client_path, SCOPES)
    creds = flow.run_local_server(port=0)
    print(f"Token obtained for scopes: {creds.scopes}")

    print("Writing CALENDAR_* vars to the scheduler service (redeploys)…")
    _railway_set(
        TARGET_SERVICE,
        CALENDAR_TOKEN_JSON=creds.to_json(),
        CALENDAR_TOKEN_PATH="/tmp/calendar_token.json",
        CALENDAR_CREDENTIALS_PATH="/tmp/calendar_credentials.json",
        CALENDAR_CREDENTIALS_JSON=creds_json,
    )
    print(
        "Done. calendar_sync (every 10 min) starts pulling the 14-day "
        "window into calendar_events. Verify: the /calendar/propose "
        "page, or scheduler logs for 'calendar_sync: N seen'."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
