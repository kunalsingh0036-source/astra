"""
Google Calendar OAuth client.

Re-uses the email-agent's OAuth app (same `gmail_credentials.json`) but
maintains a separate token file with calendar scope. Why separate:
  - Different consent screen for `calendar.readonly` — don't want to
    risk breaking the gmail token the email-agent depends on.
  - Makes revoke / re-consent easier.

First-run: if `calendar_token.json` doesn't exist, the code launches
the local OAuth server flow and pops a browser. Kunal consents once;
the token auto-refreshes forever after.

Scopes kept deliberately minimal — read-only for now. Future write
paths (create/update/delete events) will need `calendar.events` and
should only fire via the approval-gated writer (same pattern as
notes/writeback.py).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


SCOPES: list[str] = [
    "https://www.googleapis.com/auth/calendar.readonly",
    # Event create / update / delete — only ever invoked by the
    # approval-gated writeback pipeline. Read-only tools don't touch
    # this; the worker that applies approved proposals does.
    "https://www.googleapis.com/auth/calendar.events",
]

# Reuse email-agent's OAuth client (installed-app type, same GCP project).
DEFAULT_CREDENTIALS_PATH = Path(
    "/Users/kunalsingh/Claude Code/email-agent/credentials/gmail_credentials.json"
)

# Astra gets its own token file so gmail auth stays independent.
DEFAULT_TOKEN_PATH = Path(
    "/Users/kunalsingh/Claude Code/astra/credentials/calendar_token.json"
)


def get_calendar_service(
    credentials_path: Path | None = None,
    token_path: Path | None = None,
) -> Any | None:
    """Return a Calendar v3 service, or None if auth isn't set up.

    The function is synchronous — calendar syncs happen on a 10-min
    cadence so we just run it in the scheduler's thread pool.
    """
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as e:
        logger.error("[calendar] google libs missing: %s", e)
        return None

    creds_path = credentials_path or DEFAULT_CREDENTIALS_PATH
    tok_path = token_path or DEFAULT_TOKEN_PATH

    creds = None
    if tok_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(tok_path), SCOPES)
        except Exception as e:
            logger.warning("[calendar] existing token invalid: %s", e)
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                logger.warning("[calendar] refresh failed: %s", e)
                creds = None

        if creds is None or not creds.valid:
            if not creds_path.exists():
                logger.warning(
                    "[calendar] credentials file not found: %s — calendar disabled",
                    creds_path,
                )
                return None
            # First-run consent flow. Blocks on Kunal's browser.
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
            try:
                creds = flow.run_local_server(port=0)
            except Exception as e:
                logger.error("[calendar] OAuth consent failed: %s", e)
                return None

        # Save refreshed / new token
        tok_path.parent.mkdir(parents=True, exist_ok=True)
        tok_path.write_text(creds.to_json())

    try:
        return build("calendar", "v3", credentials=creds, cache_discovery=False)
    except Exception as e:
        logger.error("[calendar] service build failed: %s", e)
        return None


def is_authorized() -> bool:
    """Cheap check — true if a valid token file already exists.

    Lets callers decide whether to show "connect calendar" UI instead of
    blocking on the consent flow mid-request.
    """
    try:
        from google.oauth2.credentials import Credentials

        if not DEFAULT_TOKEN_PATH.exists():
            return False
        creds = Credentials.from_authorized_user_file(
            str(DEFAULT_TOKEN_PATH), SCOPES
        )
        return bool(creds and (creds.valid or creds.refresh_token))
    except Exception:
        return False
