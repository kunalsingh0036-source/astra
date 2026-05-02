"""
Signal extraction over the raw email corpus.

A briefing doesn't want "here are 180 unread emails" — it wants
"Ankur hasn't replied in 7 days about the pre-seed; Chinmay sent
three follow-ups this week; Kotak sent routine alerts you can
ignore."

Three derived views:

  * `daily_digest(window_hours)` — inbound in the last N hours,
    grouped by category + sender. Used by the morning/evening
    briefing and the research context bundler.

  * `unanswered_incoming(days)` — inbound messages from non-noreply
    senders that have no matching outbound reply in the same thread
    since. Ranked by age × apparent importance.

  * `top_senders_window(window_days)` — sender frequency table for
    the person-CRM bootstrap.

Heuristics here are intentionally simple. The LLM in the briefing /
research pipeline can reason over the structured output; we just
need to surface the right slices.
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from astra.email.client import list_messages

logger = logging.getLogger(__name__)


IST = timezone(timedelta(hours=5, minutes=30))


# Noise detection — senders that don't want a human reply.
#
# We check both the local-part (before @) and the full address. Patterns
# are organized so additions stay readable. Order within a tuple doesn't
# matter; each tuple is a disjunction.

# Local-part prefixes / substrings that mean "automated sender"
_NOISE_LOCAL = (
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
    "donotrespond", "automated",
    "notification", "notifications",
    "alert", "alerts",
    "update", "updates",
    "news", "newsletter",
    "receipt", "receipts", "invoice", "billing",
    "marketing", "promo", "promotions", "offer", "offers",
    "campaign", "campaigns", "newcomers",
    "transaction", "transactions",
    "statements", "statement",
    "mailer", "mailers", "mail", "email",
    "info", "hello", "team", "support",
    "care", "customercare",
    "welcome", "onboarding",
    "account", "accounts",
    "emandates", "emandate",
    "instructor", "instructors",
    "sales",
)

# Domain substrings (checked against everything after the @). High
# signal — if it matches, it's almost certainly not a human to reply to.
_NOISE_DOMAIN = (
    # Banking / payments
    "sbicard.com", "kotak.bank", "icicibank", "icici.bank",
    "hdfcbank", "hdfc.bank", "axisbank", "axis.bank",
    "yesbank", "yes.bank", "sbi.co", "sbi.bank", "bankalerts",
    "paytm.com", "phonepe.com", "gpay",
    # Govt / compliance
    "gst.gov.in", "incometax.gov.in",
    # Platforms that ping a lot
    "facebookmail.com", "facebook.com", "meta.com",
    "linkedin.com", "linkedinmail",
    "slack.com", "medium.com", "substack.com",
    "github.com", "gitlab.com",
    "googleplay", "googlecommunity", "google.com/play",
    "youtube.com", "youtu.be",
    "appleid.apple.com", "apple.com",
    "anthropic.com", "openai.com",
    "cloudflare.com", "notion.so", "figma.com",
    # Travel / retail (common auto-senders in Indian inbox)
    "booking.com", "airbnb.com",
    "spicejet.com", "web-spicejet", "indigo.in", "airindia",
    "mmt.mp.makemytrip", "makemytrip.com",
    "uber.com", "olacabs",
    "amazon.in", "amazon.com", "flipkart.com", "myntra.com",
    "reliancedigital", "relianceretail",
    "tata1mg", "emaila.1mg.com", "1mg.com",
    "swiggy.in", "zomato.com",
    # Fitness / content
    "freeletics", "updates.freeletics", "xpandstore",
    "email.intch", "intch.org",
    "myhq.in",
    # Generic "marketing email" domains
    "email.", ".campaign.", "send.", "sendgrid", "mailgun", "mailchimp",
)

# If the domain starts with any of these subdomain hints, it's a sender
# domain used only for outbound blasts — never a human.
_NOISE_SUBDOMAIN_PREFIX = (
    "updates.", "news.", "newsletter.", "email.", "mail.",
    "notifications.", "notify.", "alerts.", "info.", "marketing.",
    "campaigns.", "promo.", "offers.", "invoice.", "billing.",
    "account.", "accounts.", "noreply.", "support.",
)


def _is_noise(from_address: str) -> bool:
    s = (from_address or "").lower()
    if not s:
        return True

    # Extract local + domain from "name <a@b.c>" or "a@b.c"
    import re as _re
    m = _re.search(r"<([^>]+)>", s)
    email_part = (m.group(1) if m else s).strip()
    if "@" in email_part:
        local, _, domain = email_part.partition("@")
    else:
        local, domain = email_part, ""

    # Domain-level substring hits
    for p in _NOISE_DOMAIN:
        if p in domain:
            return True
    # Subdomain prefixes
    for p in _NOISE_SUBDOMAIN_PREFIX:
        if domain.startswith(p):
            return True
    # Local-part substring hits (word-aware: match if substring is
    # wrapped by non-letters OR appears at start/end)
    for p in _NOISE_LOCAL:
        if p in local:
            return True
    return False


def _parse_addr(addr: str) -> tuple[str, str]:
    """Split 'Name <email>' into (name, email)."""
    if not addr:
        return "", ""
    m = re.match(r"^\s*(.*?)\s*<([^>]+)>\s*$", addr)
    if m:
        return m.group(1).strip().strip('"'), m.group(2).strip().lower()
    return "", addr.strip().lower()


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────
# Daily digest
# ──────────────────────────────────────────────────────────────────


async def daily_digest(
    *,
    window_hours: int = 24,
    include_noise: bool = False,
) -> dict[str, Any]:
    """Summary of inbound activity in the last N hours.

    Returns:
      {
        "window_hours": 24,
        "total_inbound": N,
        "unread": N,
        "action_needed": N,
        "by_category": {"unclassified": N, ...},
        "notable": [
            {"from": "...", "subject": "...", "sent_at": "...",
             "snippet": "..."}, ...
        ],
        "noise_count": N
      }
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=window_hours)

    msgs = await list_messages(direction="inbound", limit=200)
    in_window = [
        m for m in msgs
        if (_parse_dt(m.get("sent_at")) or cutoff) >= cutoff
    ]

    noise = [m for m in in_window if _is_noise(m.get("from_address", ""))]
    real = [m for m in in_window if not _is_noise(m.get("from_address", ""))]
    corpus = in_window if include_noise else real

    unread = sum(1 for m in corpus if not m.get("is_read"))
    action = sum(1 for m in corpus if m.get("ai_action_needed"))
    by_cat = Counter(m.get("ai_category") or "unclassified" for m in corpus)

    # Notable: top 10 non-noise, unread-first, newest-first
    def _rank(m: dict) -> tuple[int, datetime]:
        unread_rank = 0 if m.get("is_read") else 1
        dt = _parse_dt(m.get("sent_at")) or datetime.min.replace(tzinfo=timezone.utc)
        return (unread_rank, dt)

    notable = sorted(real, key=_rank, reverse=True)[:10]
    notable_out = [
        {
            "id": m.get("id"),
            "gmail_message_id": m.get("gmail_message_id"),
            "from": m.get("from_address", ""),
            "subject": (m.get("subject") or "")[:140],
            "sent_at": m.get("sent_at"),
            "snippet": (m.get("snippet") or m.get("body_text") or "")[:240],
            "is_read": bool(m.get("is_read")),
            "action_needed": bool(m.get("ai_action_needed")),
            "category": m.get("ai_category") or "unclassified",
        }
        for m in notable
    ]

    return {
        "window_hours": window_hours,
        "total_inbound": len(in_window),
        "real_inbound": len(real),
        "noise_count": len(noise),
        "unread": unread,
        "action_needed": action,
        "by_category": dict(by_cat),
        "notable": notable_out,
    }


# ──────────────────────────────────────────────────────────────────
# Unanswered incoming
# ──────────────────────────────────────────────────────────────────


async def unanswered_incoming(days: int = 14) -> list[dict[str, Any]]:
    """Inbound messages that likely need a reply + haven't got one.

    Pairing logic: for each inbound message from a non-noise sender,
    check if *any* outbound message to the same address was sent
    after it. If not, it's unanswered.

    We don't have thread-level fidelity from the list endpoint alone
    (thread_id exists but isn't traversed here) — sender+after is a
    cheap approximation and surprisingly accurate for personal email.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

    inbound = await list_messages(direction="inbound", limit=200)
    outbound = await list_messages(direction="outbound", limit=200)

    # Index outbound sends by recipient email → latest send time.
    last_sent_to: dict[str, datetime] = {}
    for m in outbound:
        t = _parse_dt(m.get("sent_at"))
        if not t:
            continue
        for to in (m.get("to_addresses") or []):
            _, email = _parse_addr(to)
            if not email:
                continue
            prev = last_sent_to.get(email)
            if prev is None or t > prev:
                last_sent_to[email] = t

    unanswered: list[dict[str, Any]] = []
    for m in inbound:
        sent = _parse_dt(m.get("sent_at"))
        if not sent or sent < cutoff:
            continue
        frm = m.get("from_address", "")
        if _is_noise(frm):
            continue
        _, email = _parse_addr(frm)
        if not email:
            continue
        replied_at = last_sent_to.get(email)
        if replied_at and replied_at > sent:
            continue
        age_hours = (now - sent).total_seconds() / 3600
        unanswered.append({
            "id": m.get("id"),
            "gmail_message_id": m.get("gmail_message_id"),
            "from": frm,
            "from_email": email,
            "subject": (m.get("subject") or "")[:140],
            "sent_at": m.get("sent_at"),
            "age_hours": round(age_hours, 1),
            "is_read": bool(m.get("is_read")),
            "action_needed": bool(m.get("ai_action_needed")),
            "snippet": (m.get("snippet") or m.get("body_text") or "")[:240],
        })

    # Rank: action_needed first, then unread, then oldest.
    unanswered.sort(
        key=lambda m: (
            -int(bool(m["action_needed"])),
            int(bool(m["is_read"])),
            -m["age_hours"],
        )
    )
    return unanswered


# ──────────────────────────────────────────────────────────────────
# Top senders window
# ──────────────────────────────────────────────────────────────────


async def top_senders_window(
    *, window_days: int = 30, limit: int = 25,
) -> list[dict[str, Any]]:
    """Frequency table of inbound senders over a window.

    Excludes known noise. Used to seed the person-CRM with the
    people who actually take up mental space in Kunal's inbox.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    msgs = await list_messages(direction="inbound", limit=200)

    by_email: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "names": set(), "last_sent": None}
    )
    for m in msgs:
        t = _parse_dt(m.get("sent_at"))
        if not t or t < cutoff:
            continue
        frm = m.get("from_address", "")
        if _is_noise(frm):
            continue
        name, email = _parse_addr(frm)
        if not email:
            continue
        row = by_email[email]
        row["count"] += 1
        if name:
            row["names"].add(name)
        prev = row["last_sent"]
        if prev is None or t > prev:
            row["last_sent"] = t

    ranked = sorted(
        by_email.items(),
        key=lambda kv: (-kv[1]["count"], kv[0]),
    )[:limit]
    return [
        {
            "email": email,
            "count": row["count"],
            "names": sorted(row["names"]),
            "last_sent": row["last_sent"].isoformat() if row["last_sent"] else None,
        }
        for email, row in ranked
    ]
