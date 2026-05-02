"""
Share pipeline — turn a dropped payload into something Astra can reason over.

Runs on the 30s scheduler tick. For each share in state='received'
(with retry_count < MAX_RETRIES):

  1. **Extract content.** PDFs → text via pypdf. URLs → fetched + stripped
     HTML body. Audio → handed to the meetings pipeline. Images stay as
     metadata only for now (vision OCR per share is too expensive without
     an explicit user signal — easy to add later).

  2. **Classify with full context.** Haiku sees the *extracted* text +
     metadata, not just the title bar. So a shared TechCrunch article
     gets classified knowing what the article actually says.

  3. **Always file a memory.** Every share becomes an episodic memory
     regardless of action. The classification adds signal (task vs
     "just remember this") but doesn't gatekeep ingestion. This is the
     reframe: Astra learns from every signal Kunal sends, not just the
     ones the classifier flagged as "important".

  4. **Stage a task only if action='task'.** Side-effects layered on
     top of the always-memory base.

  5. **Notify on file.** macOS + push, with the right deep link.

  6. **Retry on failure.** Network blip during URL fetch / Haiku
     timeout / etc → leave row in 'received' and bump retry_count.
     Past MAX_RETRIES, mark error so it stops cycling.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

from astra.db.engine import async_session

logger = logging.getLogger(__name__)


AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".aac", ".mp4", ".mov", ".aiff", ".aif", ".flac", ".ogg", ".webm"}
RECORDINGS_DIR = Path(os.path.expanduser("~/Astra/recordings"))

# How much extracted PDF/URL text we keep on the row + feed downstream.
# Bumped to 60K because Claude's clean PDF extraction is more
# structurally informative than pypdf's letter-spaced soup, and a
# 30-page deck routinely runs 20–40K chars when properly extracted.
# Storing more here means the agent's get_share tool has the full
# document available even for long pitches / dossiers.
EXTRACT_CHAR_LIMIT = 60_000

# Haiku classification prompt budget — slice we feed the classifier
# (vs. what we cache on the row). Kept smaller than the cache: the
# classifier needs enough to decide memory/task/note, not the whole
# document. The full content is queryable later via get_share.
PROMPT_PAYLOAD_BUDGET = 16_000

# Past 5 attempts a row gets parked in 'error'. Most failures are
# transient (network, API rate limit) and clear inside one or two ticks.
MAX_RETRIES = 5

# URL fetch hard limits — don't drag down the scheduler on a hostile
# server, and don't slurp a 50MB page.
URL_TIMEOUT_S = 8.0
URL_MAX_BYTES = 2_500_000


async def tick() -> dict[str, Any]:
    processed: list[int] = []
    errored: list[int] = []
    retried: list[int] = []

    async with async_session() as s:
        r = await s.execute(
            text(
                """
                SELECT id, kind, source_app, source_url, title, text,
                       note, file_path, mime_type, retry_count
                FROM shares
                WHERE state = 'received'
                  AND retry_count < :max_r
                ORDER BY created_at ASC
                LIMIT 10
                """
            ),
            {"max_r": MAX_RETRIES},
        )
        pending = [
            {
                "id": row[0], "kind": row[1], "source_app": row[2],
                "source_url": row[3], "title": row[4], "text": row[5],
                "note": row[6], "file_path": row[7], "mime_type": row[8],
                "retry_count": row[9] or 0,
            }
            for row in r.all()
        ]

    for share in pending:
        try:
            await _advance_one(share)
            processed.append(share["id"])
        except Exception as e:
            logger.exception("[shares] row %s failed", share["id"])
            await _mark_retry_or_error(share, str(e))
            (retried if share["retry_count"] + 1 < MAX_RETRIES else errored).append(share["id"])

    return {"processed": processed, "errored": errored, "retried": retried}


async def _advance_one(share: dict[str, Any]) -> None:
    fp = share.get("file_path") or ""

    # Audio → hand off to meetings pipeline (unchanged path).
    if fp:
        ext = Path(fp).suffix.lower()
        if ext in AUDIO_EXTS:
            await _file_as_audio(share, Path(fp))
            return

    # Extract content from whatever we got. Result is appended to the
    # share's text fields for classification AND persisted to the
    # extracted_text column so future reads don't re-do the work.
    extracted = await _extract_content(share)
    if extracted:
        async with async_session() as s:
            await s.execute(
                text("UPDATE shares SET extracted_text = :ex WHERE id = :id"),
                {"id": share["id"], "ex": extracted[:EXTRACT_CHAR_LIMIT]},
            )
            await s.commit()
        share["extracted_text"] = extracted

    await _file_with_classification(share)


async def _mark_retry_or_error(share: dict[str, Any], err: str) -> None:
    """Bump retry_count; flip to 'error' when we hit MAX_RETRIES."""
    next_count = (share.get("retry_count") or 0) + 1
    if next_count >= MAX_RETRIES:
        async with async_session() as s:
            await s.execute(
                text(
                    """
                    UPDATE shares
                    SET state = 'error',
                        retry_count = :rc,
                        error = :e,
                        processed_at = now()
                    WHERE id = :id
                    """
                ),
                {"id": share["id"], "rc": next_count, "e": err[:900]},
            )
            await s.commit()
    else:
        async with async_session() as s:
            await s.execute(
                text(
                    """
                    UPDATE shares
                    SET retry_count = :rc, error = :e
                    WHERE id = :id
                    """
                ),
                {"id": share["id"], "rc": next_count, "e": err[:900]},
            )
            await s.commit()


# ── Extraction ─────────────────────────────────────────────────────


async def _extract_content(share: dict[str, Any]) -> str:
    """Pull readable text out of whatever the share carries.

    Order of operations: PDF on disk → URL fetch → return empty.
    Text/note already on the row are not re-extracted; they're handed
    straight to the classifier.
    """
    fp = share.get("file_path") or ""
    mime = (share.get("mime_type") or "").lower()

    # PDF — Anthropic's native PDF parser as primary (handles designed
    # decks correctly), pypdf as offline fallback.
    if fp and (mime == "application/pdf" or fp.lower().endswith(".pdf")):
        try:
            via_claude = await _extract_pdf_via_claude(fp)
            if via_claude:
                return via_claude
        except Exception as e:
            logger.warning("[shares] anthropic pdf extract failed for %s: %s", fp, e)
        return await asyncio.to_thread(_extract_pdf_pypdf, fp)

    # URL — fetch the page and strip to text.
    src_url = share.get("source_url") or ""
    if src_url and src_url.lower().startswith(("http://", "https://")):
        return await _fetch_url_text(src_url)

    return ""


# ── PDF extraction: Claude primary, pypdf fallback ──────────────────


# How big a PDF we'll send to Anthropic. Their docs allow up to ~32MB
# in a single document block; we cap lower because anything bigger is
# usually a fax-quality scan that's better handled by a real OCR
# pipeline (out of scope for now).
_PDF_MAX_BYTES = 25_000_000


async def _extract_pdf_via_claude(path: str) -> str:
    """Read a PDF via Claude's native document support.

    Why this over pypdf:
    Designed PDFs (Keynote, InDesign, Canva, Figma exports) typically
    render headers with explicit per-glyph letter-spacing — pypdf
    reconstructs that as "B L A C K A N D Y E L L O W" with each
    character its own token. The body text is usually fine. The
    headers being noise was making the agent waste budget reasoning
    over garbage. Claude's PDF parser handles these correctly because
    it sees the layout, not just the text stream.

    Cost is ~$0.02 per 30-page deck on Haiku (input tokens dominate).
    For Kunal's volume — a few PDFs a day — that's pennies a month
    in exchange for clean, structured output the agent can quote.
    """
    import base64
    from pathlib import Path

    p = Path(path)
    if not p.exists():
        return ""
    data = p.read_bytes()
    if len(data) > _PDF_MAX_BYTES or len(data) == 0:
        return ""

    # Reuse the same key-resolution dance as _classify so we don't
    # introduce yet another env-loading path.
    import os
    import anthropic
    from astra.config import settings

    key = settings.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        env = Path(__file__).resolve().parents[2] / ".env"
        if env.exists():
            for line in env.read_text().splitlines():
                if line.startswith("ANTHROPIC_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not key:
        return ""

    client = anthropic.AsyncAnthropic(api_key=key)
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4_000,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": base64.b64encode(data).decode(),
                    },
                },
                {
                    "type": "text",
                    "text": (
                        "Extract all text content from this document, "
                        "preserving heading structure and section "
                        "breaks. Use markdown headers (#, ##) for "
                        "section titles. Preserve bullet points and "
                        "tabular data. Do NOT summarize. Do NOT add "
                        "commentary, preamble, or 'here is the text' "
                        "framing. Output the document content directly."
                    ),
                },
            ],
        }]
    )
    out = "\n".join(b.text for b in resp.content if hasattr(b, "text"))
    return _collapse_whitespace(out)


def _extract_pdf_pypdf(path: str) -> str:
    """Offline fallback. Lower quality on designed decks but works
    without an API call — useful if Anthropic is rate-limited or down.
    """
    try:
        from pypdf import PdfReader

        reader = PdfReader(path)
        out: list[str] = []
        for page in reader.pages[:80]:  # cap pages to keep cost sane
            try:
                out.append(page.extract_text() or "")
            except Exception:
                continue
        joined = "\n".join(s.strip() for s in out if s and s.strip())
        return _collapse_whitespace(joined)
    except Exception as e:
        logger.warning("[shares] pypdf extract failed for %s: %s", path, e)
        return ""


async def _fetch_url_text(url: str) -> str:
    """Best-effort fetch + strip. Falls back to empty on any failure —
    the pipeline still has the URL string itself for classification."""
    try:
        import httpx
        from bs4 import BeautifulSoup

        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=URL_TIMEOUT_S,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Astra share fetcher) "
                    "AppleWebKit/537.36 Safari/537.36"
                ),
            },
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            ctype = (resp.headers.get("content-type") or "").lower()
            if "html" not in ctype and "text" not in ctype:
                # Don't try to parse a binary as HTML.
                return ""
            raw = resp.content[:URL_MAX_BYTES]

        soup = BeautifulSoup(raw, "html.parser")
        for tag in soup(["script", "style", "noscript", "header",
                          "footer", "nav", "aside", "form", "iframe"]):
            tag.decompose()

        title = (soup.title.string or "").strip() if soup.title else ""
        body = soup.get_text(separator="\n")
        text_out = _collapse_whitespace(body)

        if title:
            return f"{title}\n\n{text_out}"
        return text_out
    except Exception as e:
        logger.warning("[shares] url fetch failed for %s: %s", url, e)
        return ""


def _collapse_whitespace(s: str) -> str:
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


# ── Audio (unchanged hand-off) ─────────────────────────────────────


async def _file_as_audio(share: dict[str, Any], src: Path) -> None:
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    dest = RECORDINGS_DIR / f"share-{share['id']}-{src.name}"
    shutil.copy2(src, dest)

    summary = f"Audio share ({src.suffix}) filed as meeting input: {dest.name}"
    memory_id = await _file_memory(share, summary, body_override="")

    async with async_session() as s:
        await s.execute(
            text(
                """
                UPDATE shares
                SET state = 'filed',
                    summary = :sum,
                    action_taken = 'meeting',
                    memory_id = :mid,
                    processed_at = now()
                WHERE id = :id
                """
            ),
            {"id": share["id"], "sum": summary[:4000], "mid": memory_id},
        )
        await s.commit()
    await _notify_filed(share, summary, url="/meetings")


# ── Text / URL / PDF / image → classify + always-memory ─────────────


async def _file_with_classification(share: dict[str, Any]) -> None:
    """Classify with full extracted context, then:
       - always write an episodic memory
       - additionally stage a task if the classifier said 'task'

    The classifier's 'note' / 'memory' / 'task' label drives `action_taken`
    on the row (for UI badges) and whether a task gets created. It does
    NOT decide whether the share enters Astra's memory — every share does.
    """
    payload = _compose_payload(share)
    decision = await _classify(payload)

    action = decision.get("action", "note")
    summary = decision.get("summary", "")[:2000]
    task_title = decision.get("task_title") or share.get("title") or _first_line(share)

    memory_id = await _file_memory(share, summary or _first_line(share))
    task_ids: list[int] = []

    if action == "task":
        tid = await _stage_task(share, task_title, decision.get("priority", 2))
        if tid:
            task_ids = [tid]

    async with async_session() as s:
        await s.execute(
            text(
                """
                UPDATE shares
                SET state = 'filed',
                    summary = :sum,
                    action_taken = :act,
                    memory_id = :mid,
                    task_ids = CAST(:tids AS JSONB),
                    processed_at = now(),
                    error = NULL
                WHERE id = :id
                """
            ),
            {
                "id": share["id"],
                "sum": summary,
                "act": action[:63],
                "mid": memory_id,
                "tids": json.dumps(task_ids),
            },
        )
        await s.commit()

    url = "/tasks" if (action == "task" and task_ids) else "/shares"
    await _notify_filed(share, summary or f"Filed as {action}.", url=url)


def _first_line(share: dict[str, Any]) -> str:
    for key in ("title", "text", "note"):
        v = (share.get(key) or "").strip().splitlines()
        if v and v[0]:
            return v[0][:140]
    su = (share.get("source_url") or "").strip()
    if su:
        return su[:140]
    return "shared item"


def _compose_payload(share: dict[str, Any]) -> str:
    """Build the prompt body. Extracted text (if present) gets the
    biggest slice — that's the actual signal."""
    bits: list[str] = []
    if share.get("source_app"):
        bits.append(f"from: {share['source_app']}")
    if share.get("kind"):
        bits.append(f"kind: {share['kind']}")
    if share.get("source_url"):
        bits.append(f"url: {share['source_url']}")
    if share.get("title"):
        bits.append(f"title: {share['title']}")
    if share.get("note"):
        bits.append(f"note: {share['note']}")
    if share.get("text"):
        bits.append(f"text:\n{share['text'][:4000]}")
    extracted = share.get("extracted_text") or ""
    if extracted:
        bits.append(f"extracted:\n{extracted[:PROMPT_PAYLOAD_BUDGET]}")
    return "\n".join(bits) or "(empty share)"


async def _classify(payload: str) -> dict[str, Any]:
    """Ask Haiku what to do with this share.

    Falls back to 'note' on any failure so the row always lands in
    'filed' state with a reasonable summary.
    """
    try:
        import anthropic

        from astra.config import settings

        key = settings.anthropic_api_key or os.environ.get(
            "ANTHROPIC_API_KEY", ""
        )
        if not key:
            env = Path(__file__).resolve().parents[2] / ".env"
            if env.exists():
                for line in env.read_text().splitlines():
                    if line.startswith("ANTHROPIC_API_KEY="):
                        key = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
        client = anthropic.AsyncAnthropic(api_key=key)
    except Exception as e:
        logger.warning("[shares] anthropic init failed: %s", e)
        return {"action": "note", "summary": payload[:500]}

    prompt = (
        "Kunal shared this item into Astra from his phone. You're "
        "deciding what Astra should do with it.\n\n"
        "- action: one of 'task' | 'memory' | 'note'\n"
        "  * task   — there's something he or Astra needs to act on\n"
        "  * memory — meaningful context to remember (e.g. someone's\n"
        "             preference, a quote, a research source he'll\n"
        "             reference later)\n"
        "  * note   — neither; just filing\n"
        "- summary: ONE short sentence (<160 chars) describing what\n"
        "  this share is and why it matters to Kunal. Reference the\n"
        "  actual content. Avoid 'a shared item from X'.\n"
        "- task_title: imperative form if action=task ('reply to X about Y')\n"
        "- priority: 1=low, 2=normal, 3=high\n\n"
        f"<payload>\n{payload[:PROMPT_PAYLOAD_BUDGET]}\n</payload>\n\n"
        "Return STRICT JSON, no prose, no fences:\n"
        '{"action":"...","summary":"...","task_title":"...","priority":1}'
    )

    try:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        text_out = "\n".join(
            b.text for b in resp.content if hasattr(b, "text")
        ).strip()
        if text_out.startswith("```"):
            text_out = re.sub(r"^```(?:json)?|```$", "", text_out, flags=re.M).strip()
        data = json.loads(text_out)
        if data.get("action") not in ("task", "memory", "note"):
            data["action"] = "note"
        return data
    except Exception as e:
        # IMPORTANT: re-raise so the row stays in 'received' and the
        # outer tick() bumps retry_count. We *do* want transient API
        # failures retried, not silently downgraded to a meaningless
        # "filed as note" with a 200-char dump as the summary.
        logger.warning("[shares] classify failed: %s", e)
        raise


async def _file_memory(
    share: dict[str, Any],
    summary: str,
    *,
    body_override: str | None = None,
) -> int | None:
    """Write an episodic memory for this share. Always called.

    The memory body is the full classification payload — extracted PDF
    text, fetched URL body, the lot — so semantic search later can
    surface a share by its actual content, not just its title.
    """
    try:
        from astra.memory.models import MemoryType
        from astra.memory.store import store_memory

        body = body_override if body_override is not None else _compose_payload(share)
        async with async_session() as s:
            mem = await store_memory(
                session=s,
                content=f"Share — {summary}\n\n{body[:8000]}",
                memory_type=MemoryType.EPISODIC,
                source="share",
                tags=_tags_for_share(share),
                importance=_importance_for_share(share),
            )
            await s.commit()
            return int(getattr(mem, "id", 0)) or None
    except Exception as e:
        logger.warning("[shares] file_memory failed: %s", e)
        return None


def _tags_for_share(share: dict[str, Any]) -> str:
    tags = ["share", "iphone"]
    kind = (share.get("kind") or "").strip().lower()
    if kind:
        tags.append(kind)
    src = (share.get("source_app") or "").strip().lower()
    # Cheap normalization — strip the obvious noise.
    if src and src not in ("ios share sheet", "share sheet", ""):
        tags.append(re.sub(r"[^a-z0-9]+", "-", src).strip("-")[:32])
    return ",".join(tags)


def _importance_for_share(share: dict[str, Any]) -> float:
    """Bias importance up when the share carries real content.

    A bare URL is 0.4 — Astra should remember it but not surface it
    aggressively. A PDF Kunal hand-shared with extracted text is 0.7
    — that's a deliberate gesture worth weighting."""
    if share.get("extracted_text"):
        return 0.7
    if (share.get("text") or "").strip():
        return 0.6
    if share.get("source_url"):
        return 0.4
    return 0.5


async def _stage_task(
    share: dict[str, Any], title: str, priority: int,
) -> int | None:
    try:
        prio = max(1, min(3, int(priority or 2)))
    except Exception:
        prio = 2

    title = (title or _first_line(share))[:511]
    note_bits: list[str] = []
    if share.get("source_app"):
        note_bits.append(f"via {share['source_app']}")
    if share.get("source_url"):
        note_bits.append(share["source_url"])
    if share.get("note"):
        note_bits.append(share["note"])
    note = " · ".join(note_bits)[:2000]

    async with async_session() as s:
        r = await s.execute(
            text(
                """
                INSERT INTO tasks (title, note, status, priority, tags, source)
                VALUES (:t, :n, 'open', :p, 'share', :src)
                RETURNING id
                """
            ),
            {
                "t": title,
                "n": note,
                "p": prio,
                "src": f"share:{share['id']}",
            },
        )
        tid = int(r.scalar_one())
        await s.commit()
        return tid


async def _notify_filed(
    share: dict[str, Any], summary: str, url: str = "/shares",
) -> None:
    try:
        from astra.notifications import notify

        title = "astra · share filed"
        notify(
            title=title,
            subtitle=share.get("source_app") or share.get("kind", "share"),
            body=summary[:160] or "saved",
            url=url,
            tag=f"share-{share['id']}",
        )
    except Exception as e:
        logger.warning("[shares] notify failed: %s", e)
