"""
One-time (re-runnable) backfill of Kunal's SENT history.

Why: the routine sync bootstraps `newer_than:7d` — built for digests,
not learning. The store held 20 outbound messages total (2026-07-03),
so every "learn Kunal's voice" loop had an empty tank. His full sent
history is the single richest record of how he actually writes, per
recipient, per register — this pulls it in so the voice miner has a
real corpus.

Design constraints honored:
- The Google client is SYNCHRONOUS: a full backfill is thousands of
  sequential HTTP gets (minutes). Everything Gmail-side runs in a
  worker thread (asyncio.to_thread) so the service event loop — and
  its /health — never blocks (the embedding cold-load lesson).
- Runs as a fire-and-forget background task with module-level progress
  state; the route returns immediately and a status endpoint reports
  progress. Double-starts are rejected.
- Storage reuses the sync path's dedupe (`_store_parsed`), committing
  in batches on isolated sessions.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from email_agent.db.engine import async_session
from email_agent.models.account import EmailAccount

logger = logging.getLogger(__name__)

_PAGE_SIZE = 100          # Gmail list page (IDs); gets happen per-message
_DEFAULT_MAX = 2000       # bound the one-time pull; re-run raises coverage

# Module-level progress so GET /backfill-sent reports honestly.
state: dict = {"running": False, "fetched": 0, "stored": 0,
               "max": 0, "started_at": None, "finished_at": None,
               "error": None}


def _fetch_page_sync(query: str, page_token: str | None, page_size: int):
    """One Gmail page: list IDs then full-get each. Pure sync — runs in
    a worker thread. Returns (parsed_msgs, next_page_token)."""
    from email_agent.services.gmail_client import (
        _get_gmail_service,
        _parse_gmail_message,
    )

    service = _get_gmail_service()
    if not service:
        raise RuntimeError("gmail service unavailable (creds?)")
    params = {"userId": "me", "maxResults": page_size, "q": query}
    if page_token:
        params["pageToken"] = page_token
    listing = service.users().messages().list(**params).execute()
    refs = listing.get("messages", [])
    parsed = []
    for ref in refs:
        try:
            msg = service.users().messages().get(
                userId="me", id=ref["id"], format="full"
            ).execute()
            parsed.append(_parse_gmail_message(msg))
        except Exception as e:  # skip a bad message, keep the run alive
            logger.warning("[backfill] get %s failed: %s", ref.get("id"), e)
    return parsed, listing.get("nextPageToken")


async def _run(max_messages: int, query: str) -> None:
    from email_agent.services.sync_service import _store_parsed

    try:
        async with async_session() as s:
            r = await s.execute(
                select(EmailAccount).where(EmailAccount.is_active == True)  # noqa: E712
            )
            account = r.scalars().first()
        if account is None:
            raise RuntimeError("no active email account")

        page_token: str | None = None
        while state["fetched"] < max_messages:
            page = min(_PAGE_SIZE, max_messages - state["fetched"])
            parsed, page_token = await asyncio.to_thread(
                _fetch_page_sync, query, page_token, page
            )
            if not parsed:
                break
            state["fetched"] += len(parsed)
            # Store this page on its own session/commit.
            async with async_session() as s:
                for p in parsed:
                    try:
                        if await _store_parsed(account.id, p, s):
                            state["stored"] += 1
                    except Exception as e:
                        logger.warning("[backfill] store failed: %s", e)
                await s.commit()
            logger.info(
                "[backfill] progress: fetched=%d stored=%d",
                state["fetched"], state["stored"],
            )
            if not page_token:
                break
        logger.info(
            "[backfill] DONE: fetched=%d stored(new)=%d",
            state["fetched"], state["stored"],
        )
    except Exception as e:
        state["error"] = str(e)[:300]
        logger.error("[backfill] failed: %s", e)
    finally:
        state["running"] = False
        state["finished_at"] = datetime.now(timezone.utc).isoformat()


def start_backfill(max_messages: int = _DEFAULT_MAX, query: str = "in:sent") -> dict:
    """Kick off the background backfill. Returns immediately."""
    if state["running"]:
        return {"ok": False, "error": "backfill already running", **_progress()}
    state.update(running=True, fetched=0, stored=0, max=max_messages,
                 started_at=datetime.now(timezone.utc).isoformat(),
                 finished_at=None, error=None)
    # Keep a strong reference — an unreferenced task can be GC-cancelled
    # mid-run. Stored on the module state dict for the process lifetime.
    state["_task"] = asyncio.create_task(_run(max_messages, query))
    return {"ok": True, "started": True, **_progress()}


def _progress() -> dict:
    return {k: state[k] for k in
            ("running", "fetched", "stored", "max", "started_at",
             "finished_at", "error")}


def backfill_status() -> dict:
    return {"ok": True, **_progress()}
