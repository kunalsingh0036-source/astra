#!/usr/bin/env python3
"""
Re-authorize Gmail for the cloud email agent — one command, ~30s.

Why this exists: the email agent's Gmail refresh token dies whenever
the GCP OAuth app is in "Testing" status (tokens expire after 7 days)
or consent is revoked. Symptom in the email service logs:
    Failed to initialize Gmail API: ('invalid_grant: Bad Request', ...)
and the email_sync scheduler job reporting bootstrap failures.

What it does:
  1. Pulls GMAIL_CREDENTIALS_JSON (the OAuth client, not a user
     token) from the Railway email service via the railway CLI.
  2. Runs Google's local-server consent flow — your browser opens,
     you pick kunalsingh0036@gmail.com, click Allow.
  3. Writes the fresh token back to Railway as GMAIL_TOKEN_JSON and
     redeploys the email service.

After it finishes, the scheduler's email_sync job (every 5 min)
bootstraps the account and backfills automatically — no further
action needed.

PERMANENT FIX (do once, kills this whole failure mode): GCP console
→ APIs & Services → OAuth consent screen → Publish app. Published
apps' refresh tokens don't expire weekly.

Usage:
    python3 scripts/gmail_reauth.py
Requires: railway CLI logged in; pip install google-auth-oauthlib
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

# MUST match gmail_client.py's SCOPES exactly — a scope mismatch makes
# google-auth invalidate the stored token on load.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.labels",
]
SERVICE = "email"
REPO_ROOT = Path(__file__).resolve().parents[1]


def _railway_get(var: str) -> str:
    out = subprocess.run(
        ["railway", "variables", "--service", SERVICE, "--kv"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=True,
    ).stdout
    for line in out.splitlines():
        if line.startswith(f"{var}="):
            return line.split("=", 1)[1]
    return ""


def main() -> int:
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("pip install google-auth-oauthlib  — then rerun", file=sys.stderr)
        return 2

    creds_json = _railway_get("GMAIL_CREDENTIALS_JSON")
    if not creds_json:
        print(
            "GMAIL_CREDENTIALS_JSON not found on the Railway email "
            "service — set it first (OAuth client JSON from GCP console).",
            file=sys.stderr,
        )
        return 1

    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False
    ) as f:
        f.write(creds_json)
        client_path = f.name

    print("Opening browser for Google consent…")
    flow = InstalledAppFlow.from_client_secrets_file(client_path, SCOPES)
    creds = flow.run_local_server(port=0)
    token_json = creds.to_json()
    print(f"Token obtained for scopes: {creds.scopes}")

    print("Writing GMAIL_TOKEN_JSON to Railway (triggers redeploy)…")
    subprocess.run(
        [
            "railway",
            "variables",
            "--service",
            SERVICE,
            "--set",
            f"GMAIL_TOKEN_JSON={token_json}",
        ],
        cwd=REPO_ROOT,
        check=True,
    )
    print(
        "Done. The email service redeploys with the fresh token; the "
        "scheduler's email_sync job bootstraps + backfills within ~5 min.\n"
        "Verify: curl -s https://email.thearrogantclub.com/api/v1/messages/summary "
        '-H "x-astra-secret: $AGENT_SHARED_SECRET"'
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
