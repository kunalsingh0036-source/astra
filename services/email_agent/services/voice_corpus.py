"""
Voice corpus — ingest Kunal's WhatsApp/Instagram chat exports so the
miner can learn his TEXTING voice, not just his email voice.

Why exports (not APIs): no official API can read a personal WhatsApp or
personal Instagram account — the ONLY safe, ToS-clean way to get his
real conversational corpus is the platforms' own export features:
  - WhatsApp: chat → Export Chat (without media) → .txt transcript
  - Instagram: Download Your Information → messages as JSON
He hands the files to Astra (bridge read or paste); this module parses
them, keeps ONLY HIS OWN messages, and stores them in
`voice_corpus_messages`. The miner then distills per-channel profiles
(register 'whatsapp_personal' / 'instagram') exactly like the email
registers.

Parsing is deliberately tolerant (Android + iOS WhatsApp formats, the
Instagram latin-1/utf-8 mojibake bug) and lossy-by-design: system
lines, media stubs, deleted messages, and likes are dropped. Dedupe by
content hash so re-ingesting the same export is a no-op.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone

from sqlalchemy import text

from email_agent.db.engine import async_session

logger = logging.getLogger(__name__)

CORPUS_CHANNELS = ("whatsapp_personal", "instagram")

_ensured = False
_ENSURE = [
    text(
        "CREATE TABLE IF NOT EXISTS voice_corpus_messages ("
        "id SERIAL PRIMARY KEY, "
        "channel TEXT NOT NULL, "
        "contact TEXT NOT NULL DEFAULT '', "
        "body TEXT NOT NULL, "
        "sent_at TIMESTAMPTZ, "
        "content_hash TEXT NOT NULL, "
        "created_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
        "CONSTRAINT uq_voice_corpus UNIQUE (channel, content_hash))"
    ),
    text(
        "CREATE INDEX IF NOT EXISTS ix_voice_corpus_channel "
        "ON voice_corpus_messages (channel)"
    ),
]


async def ensure_corpus_table() -> bool:
    global _ensured
    if _ensured:
        return True
    try:
        async with async_session() as s:
            for ddl in _ENSURE:
                await s.execute(ddl)
            await s.commit()
        _ensured = True
        return True
    except Exception as e:
        logger.warning("[voice_corpus] ensure failed: %s", e)
        return False


# ── WhatsApp .txt export ────────────────────────────────────────────
# Android: "12/07/25, 9:14 pm - Kunal Singh: message"
# iOS:     "[12/07/25, 9:14:33 PM] Kunal Singh: message"
#
# The hard rule (corpus-integrity, per adversarial review): ANY line
# whose start looks like a WhatsApp timestamp is a NEW-message BOUNDARY,
# even if we can't fully parse it. Only a boundary that STRICTLY matches
# "<ts> ... <self_name>: <body>" is kept as Kunal's. Everything else —
# other people's messages, system lines, unparsed headers — resets the
# state to None. This makes it impossible for someone else's text to be
# appended as a continuation of his message (the poisoning bug).

# Loose: does this line START a new message? (date + time near the start)
_WA_HEADER_START = re.compile(
    r"^‎?‏?\[?\s*\d{1,2}[./-]\d{1,2}[./-]\d{2,4},?\s+\d{1,2}[:.]\d{2}"
)
# Timestamp capture (shared by both platform shapes).
_WA_TS = (
    r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4}),?\s+(\d{1,2}[:.]\d{2})(?:[:.]\d{2})?"
    r"\s*([apAP]\.?[mM]\.?)?"
)
# Stub = the WHOLE message is a media/system placeholder. Anchored so a
# real message merely CONTAINING these words is never dropped.
_WA_STUB = re.compile(
    r"^‎?\s*("
    r"<media omitted>|image omitted|video omitted|audio omitted|"
    r"sticker omitted|gif omitted|document omitted|contact card omitted|"
    r"this message was deleted|you deleted this message|null|"
    r"live location shared|missed voice call|missed video call|"
    r"‎?document\b.*omitted"
    r")\s*$",
    re.IGNORECASE,
)


def _wa_is_stub(body: str) -> bool:
    return bool(_WA_STUB.match(body.strip()))


def _wa_ts(d: str, t: str, ap: str | None) -> datetime | None:
    """Best-effort timestamp (mining doesn't require it). Honors the
    am/pm marker so PM messages aren't stored 12h early; tries dd/mm
    (India) then mm/dd. Returns None if unparseable either way."""
    d = d.replace("-", "/").replace(".", "/")   # normalize date separators
    t = t.replace(".", ":")
    ap = (ap or "").replace(".", "").upper().strip()
    if ap in ("AM", "PM"):
        stamp = f"{d} {t} {ap}"
        fmts = ("%d/%m/%y %I:%M %p", "%d/%m/%Y %I:%M %p",
                "%m/%d/%y %I:%M %p", "%m/%d/%Y %I:%M %p")
    else:
        stamp = f"{d} {t}"
        fmts = ("%d/%m/%y %H:%M", "%d/%m/%Y %H:%M",
                "%m/%d/%y %H:%M", "%m/%d/%Y %H:%M")
    for fmt in fmts:
        try:
            return datetime.strptime(stamp, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def parse_whatsapp_txt(raw: str, self_name: str) -> list[dict]:
    """Extract KUNAL'S OWN messages from a WhatsApp chat export."""
    want = (self_name or "").strip()
    if not want:
        return []
    esc = re.escape(want)
    # STRICT self-message headers, exact name + ':' (no colon-truncation).
    self_android = re.compile(
        r"^‎?‏?" + _WA_TS + r"\s+-\s+" + esc + r":\s?(.*)$", re.IGNORECASE
    )
    self_ios = re.compile(
        r"^‎?‏?\[" + _WA_TS + r"\]\s+" + esc + r":\s?(.*)$", re.IGNORECASE
    )
    out: list[dict] = []
    current: dict | None = None
    for raw_line in (raw or "").replace("\r\n", "\n").split("\n"):
        line = raw_line.lstrip("﻿").strip("‎‏ \t")
        if _WA_HEADER_START.match(line):
            # Message boundary — flush whatever we held, then decide anew.
            if current is not None:
                out.append(current)
                current = None
            m = self_android.match(line) or self_ios.match(line)
            if m:
                body = (m.group(4) or "").strip("‎‏ ").strip()
                if body and not _wa_is_stub(body):
                    current = {"body": body,
                               "sent_at": _wa_ts(m.group(1), m.group(2), m.group(3))}
            # not a self header → current stays None (someone else / system)
        elif current is not None:
            # genuine continuation of HIS message (no header shape at all)
            if line:
                current["body"] += "\n" + line
    if current is not None:
        out.append(current)
    return [m for m in out if len(m["body"].strip()) >= 1]


# ── Instagram JSON export ───────────────────────────────────────────
_IG_SKIP = re.compile(
    r"^\s*("
    r"liked a message|reacted .{1,40} to your message|"
    r"you sent an attachment\.?|.{0,40} sent an attachment\.?|"
    r"you shared (a story|a post|a reel|an? .{0,20})|"
    r".{0,40} shared (a story|a post|a reel)"
    r")\s*$",
    re.IGNORECASE,
)


def _fix_ig_mojibake(s: str) -> str:
    """Instagram exports double-encode UTF-8 as Latin-1 (the famous
    'à¤' bug — Hindi/emoji arrive garbled). Round-trip decode when the
    text survives it; otherwise return as-is."""
    try:
        fixed = s.encode("latin-1").decode("utf-8")
        return fixed
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def parse_instagram_json(raw: str, self_name: str) -> list[dict]:
    """Extract KUNAL'S OWN messages from an Instagram messages_*.json
    (single thread) or a JSON array of such threads."""
    want = (self_name or "").strip().lower()
    try:
        data = json.loads(raw)
    except Exception:
        return []
    threads = data if isinstance(data, list) else [data]
    out: list[dict] = []
    for th in threads:
        if not isinstance(th, dict):
            continue
        msgs = th.get("messages")
        if not isinstance(msgs, list):
            continue
        for msg in msgs:
            if not isinstance(msg, dict):
                continue
            sender = _fix_ig_mojibake(str(msg.get("sender_name") or "")).strip().lower()
            if sender != want:
                continue
            content = msg.get("content")
            if not content or not isinstance(content, str):
                continue
            content = _fix_ig_mojibake(content).strip()
            if not content or _IG_SKIP.search(content) or len(content) < 2:
                continue
            ts = None
            if msg.get("timestamp_ms"):
                try:
                    ts = datetime.fromtimestamp(
                        int(msg["timestamp_ms"]) / 1000, tz=timezone.utc
                    )
                except Exception:
                    ts = None
            out.append({"body": content, "sent_at": ts})
    return out


# ── Ingest ──────────────────────────────────────────────────────────

async def ingest_export(
    *,
    channel: str,
    fmt: str,
    content: str,
    self_name: str,
    contact: str = "",
    dry_run: bool = False,
) -> dict:
    """Parse one export and store Kunal's own messages. Idempotent
    (hash dedupe). dry_run parses + counts without writing — the safe
    way to verify a file's format before committing it to the corpus."""
    if channel not in CORPUS_CHANNELS:
        return {"ok": False, "error": f"channel must be one of {CORPUS_CHANNELS}"}
    if not (self_name or "").strip():
        return {"ok": False, "error": "self_name required (your display name "
                                      "exactly as it appears in the export)"}
    if fmt == "whatsapp_txt":
        msgs = parse_whatsapp_txt(content, self_name)
    elif fmt == "instagram_json":
        msgs = parse_instagram_json(content, self_name)
    else:
        return {"ok": False, "error": "format must be whatsapp_txt or instagram_json"}

    if dry_run:
        sample = [m["body"][:80] for m in msgs[:5]]
        return {"ok": True, "dry_run": True, "parsed": len(msgs),
                "sample": sample}

    if not await ensure_corpus_table():
        return {"ok": False, "error": "corpus table unavailable"}

    # De-dupe within THIS export by hash so the frequency signal survives
    # (identical texts across different chats are kept via the per-message
    # nature of exports, but an exact repeat inside one file is one row).
    rows = []
    seen: set = set()
    for m in msgs:
        body = m["body"][:8000]
        h = hashlib.md5(body.encode("utf-8")).hexdigest()
        if h in seen:
            continue
        seen.add(h)
        rows.append({"c": channel, "ct": (contact or "")[:120],
                     "b": body, "ts": m["sent_at"], "h": h})

    async def _count() -> int:
        async with async_session() as s:
            r = await s.execute(
                text("SELECT count(*) FROM voice_corpus_messages WHERE channel=:c"),
                {"c": channel})
            return r.scalar() or 0

    before = await _count()
    if rows:
        # ONE round-trip (executemany) instead of thousands.
        async with async_session() as s:
            await s.execute(
                text(
                    "INSERT INTO voice_corpus_messages "
                    "(channel, contact, body, sent_at, content_hash) "
                    "VALUES (:c, :ct, :b, :ts, :h) "
                    "ON CONFLICT (channel, content_hash) DO NOTHING"
                ),
                rows,
            )
            await s.commit()
    total = await _count()
    inserted = max(0, total - before)
    logger.info("[voice_corpus] ingested %s: parsed=%d new=%d total=%d",
                channel, len(msgs), inserted, total)
    return {"ok": True, "parsed": len(msgs), "new": inserted,
            "duplicates": len(msgs) - inserted, "channel_total": total}


async def corpus_counts() -> dict:
    """Per-channel corpus sizes for status surfaces."""
    try:
        await ensure_corpus_table()
        async with async_session() as s:
            r = await s.execute(text(
                "SELECT channel, count(*) FROM voice_corpus_messages GROUP BY 1"))
            return {row[0]: row[1] for row in r.all()}
    except Exception:
        return {}
