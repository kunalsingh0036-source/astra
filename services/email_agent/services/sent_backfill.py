"""
One-time (re-runnable) backfill of Kunal's SENT history.

Why: the routine sync bootstraps `newer_than:7d` — built for digests,
not learning. The store held 20 outbound messages total (2026-07-03),
so every "learn Kunal's voice" loop had an empty tank. His full sent
history is the single richest record of how he actually writes, per
recipient, per register — this pulls it in so the voice miner has a
real corpus.

Hardened per adversarial review (21 findings):
- The Google client is SYNCHRONOUS: all Gmail I/O runs in a worker
  thread (asyncio.to_thread) so the event loop / health never blocks.
- Fire-and-forget task holds a strong module reference (GC-cancel
  guard), has a hard runtime ceiling so `running` can't wedge True
  forever, and a done-callback records any silent death.
- Per-message SAVEPOINT: one bad row rolls back that message only —
  never the page, never the run (parse-side truncation kills the main
  over-length class at the source too).
- Page commit failure logs + continues on a fresh session next page.
- Already-stored IDs are skipped BEFORE the expensive per-message
  gets, so re-runs are cheap and effectively resume.
- All-gets-failed pages are counted, not mistaken for end-of-results.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select, text

from email_agent.db.engine import async_session
from email_agent.models.account import EmailAccount

logger = logging.getLogger(__name__)

_PAGE_SIZE = 100
_DEFAULT_MAX = 2000
_CEILING_SEC = 2700  # 45 min hard cap — `finally` must always fire

state: dict = {"running": False, "listed": 0, "skipped_existing": 0,
               "fetched": 0, "stored": 0, "get_failures": 0,
               "page_errors": 0, "max": 0, "started_at": None,
               "finished_at": None, "error": None}


def _list_page_sync(query: str, page_token: str | None, page_size: int):
    """List one page of message IDs. Sync — runs in a worker thread."""
    from email_agent.services.gmail_client import _get_gmail_service

    service = _get_gmail_service()
    if not service:
        raise RuntimeError("gmail service unavailable (creds?)")
    params = {"userId": "me", "maxResults": page_size, "q": query}
    if page_token:
        params["pageToken"] = page_token
    listing = service.users().messages().list(**params).execute()
    return listing.get("messages", []), listing.get("nextPageToken")


def _get_messages_sync(ids: list[str]):
    """Full-get each ID. Sync — worker thread. Returns (parsed, failures)."""
    from email_agent.services.gmail_client import (
        _get_gmail_service,
        _parse_gmail_message,
    )

    service = _get_gmail_service()
    if not service:
        raise RuntimeError("gmail service unavailable (creds?)")
    parsed, failures = [], 0
    for mid in ids:
        try:
            msg = service.users().messages().get(
                userId="me", id=mid, format="full"
            ).execute()
            parsed.append(_parse_gmail_message(msg))
        except Exception as e:
            failures += 1
            logger.warning("[backfill] get %s failed: %s", mid, e)
    return parsed, failures


async def _run_inner(max_messages: int, query: str) -> None:
    from email_agent.models.email_message import EmailMessage
    from email_agent.services.sync_service import _store_parsed

    async with async_session() as s:
        r = await s.execute(
            select(EmailAccount).where(EmailAccount.is_active == True)  # noqa: E712
        )
        account = r.scalars().first()
    if account is None:
        raise RuntimeError("no active email account")

    page_token: str | None = None
    while state["listed"] < max_messages:
        page = min(_PAGE_SIZE, max_messages - state["listed"])
        refs, page_token = await asyncio.to_thread(
            _list_page_sync, query, page_token, page
        )
        if not refs:
            break  # true end of results
        state["listed"] += len(refs)
        ids = [ref["id"] for ref in refs]

        # Resume cheaply: skip IDs we already hold BEFORE the gets.
        try:
            async with async_session() as s:
                r = await s.execute(
                    select(EmailMessage.gmail_message_id).where(
                        EmailMessage.gmail_message_id.in_(ids)
                    )
                )
                have = {row[0] for row in r.all()}
        except Exception as e:
            logger.warning("[backfill] existing-check failed: %s", e)
            have = set()
        missing = [i for i in ids if i not in have]
        state["skipped_existing"] += len(have)
        if not missing:
            if not page_token:
                break
            continue

        parsed, failures = await asyncio.to_thread(_get_messages_sync, missing)
        state["get_failures"] += failures
        state["fetched"] += len(parsed)
        if failures and not parsed:
            logger.error("[backfill] entire page of gets failed — continuing")

        # Store the page: per-message savepoint so one bad row can never
        # poison the page transaction; count only after commit succeeds.
        page_stored = 0
        try:
            async with async_session() as s:
                for p in parsed:
                    try:
                        async with s.begin_nested():
                            if await _store_parsed(account.id, p, s):
                                page_stored += 1
                    except Exception as e:
                        logger.warning(
                            "[backfill] store %s failed (rolled back row): %s",
                            p.get("gmail_message_id"), e,
                        )
                await s.commit()
            state["stored"] += page_stored
        except Exception as e:
            state["page_errors"] += 1
            logger.error("[backfill] page commit failed (continuing): %s", e)

        logger.info(
            "[backfill] progress: listed=%d skipped=%d fetched=%d stored=%d",
            state["listed"], state["skipped_existing"],
            state["fetched"], state["stored"],
        )
        if not page_token:
            break

    logger.info(
        "[backfill] DONE: listed=%d skipped=%d fetched=%d stored(new)=%d "
        "get_failures=%d page_errors=%d",
        state["listed"], state["skipped_existing"], state["fetched"],
        state["stored"], state["get_failures"], state["page_errors"],
    )


async def _run(max_messages: int, query: str) -> None:
    try:
        await asyncio.wait_for(
            _run_inner(max_messages, query), timeout=_CEILING_SEC
        )
    except asyncio.TimeoutError:
        state["error"] = f"hit {_CEILING_SEC}s ceiling — re-run to resume"
        logger.error("[backfill] %s", state["error"])
    except Exception as e:
        state["error"] = str(e)[:300]
        logger.error("[backfill] failed: %s", e)
    finally:
        state["running"] = False
        state["finished_at"] = datetime.now(timezone.utc).isoformat()


def _on_done(task: asyncio.Task) -> None:
    exc = task.exception() if not task.cancelled() else None
    if exc:
        state["error"] = state["error"] or str(exc)[:300]
        state["running"] = False
        logger.error("[backfill] task died: %s", exc)


def start_backfill(max_messages: int = _DEFAULT_MAX, query: str = "in:sent") -> dict:
    """Kick off the background backfill. Returns immediately."""
    if state["running"]:
        return {"ok": False, "error": "backfill already running", **_progress()}
    state.update(running=True, listed=0, skipped_existing=0, fetched=0,
                 stored=0, get_failures=0, page_errors=0, max=max_messages,
                 started_at=datetime.now(timezone.utc).isoformat(),
                 finished_at=None, error=None)
    # get_running_loop fails LOUDLY outside a loop; the strong reference
    # on the module state dict prevents GC-cancellation mid-run.
    task = asyncio.get_running_loop().create_task(_run(max_messages, query))
    task.add_done_callback(_on_done)
    state["_task"] = task
    return {"ok": True, "started": True, **_progress()}


def _progress() -> dict:
    return {k: state[k] for k in
            ("running", "listed", "skipped_existing", "fetched", "stored",
             "get_failures", "page_errors", "max", "started_at",
             "finished_at", "error")}


def backfill_status() -> dict:
    return {"ok": True, **_progress()}
