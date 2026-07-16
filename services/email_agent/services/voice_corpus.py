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
# Continuation lines (no timestamp prefix) belong to the previous
# message. Locale variants tolerated: 24h times, dotted a.m./p.m.,
# unicode LRM/RLM markers Apple loves to sprinkle in.
_WA_ANDROID = re.compile(
    r"^(\d{1,2}/\d{1,2}/\d{2,4}),?\s+(\d{1,2}:\d{2})(?:\s?[apAP]\.?[mM]\.?)?"
    r"\s+-\s+([^:]{1,60}?):\s(.*)$"
)
_WA_IOS = re.compile(
    r"^\[(\d{1,2}/\d{1,2}/\d{2,4}),?\s+(\d{1,2}:\d{2})(?::\d{2})?(?:\s?[apAP][mM])?\]\s+"
    r"([^:]{1,60}?):\s(.*)$"
)
_WA_SKIP = re.compile(
    r"(<media omitted>|image omitted|video omitted|audio omitted|"
    r"sticker omitted|gif omitted|document omitted|contact card omitted|"
    r"this message was deleted|you deleted this message|"
    r"messages and calls are end-to-end encrypted|"
    r"missed voice call|missed video call|^null$|live location shared|"
    r"created group|added you|changed the subject|changed this group)",
    re.IGNORECASE,
)


def _wa_ts(d: str, t: str) -> datetime | None:
    """Best-effort timestamp. WhatsApp exports don't disambiguate
    dd/mm vs mm/dd — India exports are dd/mm; if the parse is
    impossible either way we return None (mining doesn't need it)."""
    for fmt in ("%d/%m/%y %H:%M", "%d/%m/%Y %H:%M", "%m/%d/%y %H:%M",
                "%m/%d/%Y %H:%M"):
        try:
            return datetime.strptime(f"{d} {t}", fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def parse_whatsapp_txt(raw: str, self_name: str) -> list[dict]:
    """Extract KUNAL'S OWN messages from a WhatsApp chat export."""
    want = (self_name or "").strip().lower()
    out: list[dict] = []
    current: dict | None = None
    for line in (raw or "").replace("\r\n", "\n").split("\n"):
        line = line.strip("‎‏ \t")
        m = _WA_ANDROID.match(line) or _WA_IOS.match(line)
        if m:
            # flush previous
            if current is not None:
                out.append(current)
                current = None
            d, t, sender, body = m.group(1), m.group(2), m.group(3), m.group(4)
            body = body.strip("‎‏ ").strip()
            if sender.strip().lower() != want:
                continue
            if not body or _WA_SKIP.search(body):
                continue
            current = {"body": body, "sent_at": _wa_ts(d, t)}
        elif current is not None:
            # continuation line of a multiline message from self
            if line and not _WA_SKIP.search(line):
                current["body"] += "\n" + line
    if current is not None:
        out.append(current)
    return [m for m in out if len(m["body"].strip()) >= 2]


# ── Instagram JSON export ───────────────────────────────────────────
_IG_SKIP = re.compile(
    r"(^liked a message$|^reacted .{1,40} to your message$|"
    r"sent an attachment|shared a story|shared a post|shared a reel|"
    r"^you sent an attachment)",
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
        for msg in (th or {}).get("messages", []) or []:
            sender = _fix_ig_mojibake(str(msg.get("sender_name") or ""))
            if sender.strip().lower() != want:
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

    inserted = 0
    async with async_session() as s:
        for m in msgs:
            h = hashlib.md5(m["body"].encode("utf-8")).hexdigest()
            r = await s.execute(
                text(
                    "INSERT INTO voice_corpus_messages "
                    "(channel, contact, body, sent_at, content_hash) "
                    "VALUES (:c, :ct, :b, :ts, :h) "
                    "ON CONFLICT (channel, content_hash) DO NOTHING"
                ),
                {"c": channel, "ct": (contact or "")[:120],
                 "b": m["body"][:8000], "ts": m["sent_at"], "h": h},
            )
            inserted += r.rowcount or 0
        await s.commit()

    total = 0
    try:
        async with async_session() as s:
            r = await s.execute(
                text("SELECT count(*) FROM voice_corpus_messages WHERE channel=:c"),
                {"c": channel})
            total = r.scalar() or 0
    except Exception:
        pass
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
