"""AI email classifier — categorizes, prioritizes, and summarizes emails.

Uses Claude Haiku for speed and cost ($1/MTok input).
Classifies along 3 axes:
1. Category: what kind of email is this
2. Priority: how urgent
3. Action needed: does Kunal need to respond/act

Why single-pass classification (not multi-call):
- One API call is faster than three
- Haiku can handle structured multi-field output reliably
- Keeps latency under 300ms per email
"""

import logging
import os
from pathlib import Path

import anthropic

from email_agent.config import settings

logger = logging.getLogger(__name__)


def _resolve_api_key() -> str:
    """Resolve the Anthropic key across pydantic-settings quirks.

    pydantic-settings prefers an empty env var over a non-empty .env
    value, which is exactly the state `astra up` passes to child
    processes. Fall back to reading the .env file directly so this
    never silently returns empty.
    """
    key = (settings.anthropic_api_key or "").strip()
    if key:
        return key
    env_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if env_key:
        return env_key
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("ANTHROPIC_API_KEY="):
                return (
                    line.split("=", 1)[1].strip().strip('"').strip("'")
                )
    return ""

CATEGORIES = [
    "client",           # From/about clients
    "team",             # Internal team communication
    "vendor",           # From vendors/suppliers
    "investor",         # Investor relations
    "legal",            # Legal/compliance
    "financial",        # Bank, tax, accounting
    "recruitment",      # Hiring/job related
    "newsletter",       # Newsletters/marketing emails
    "notification",     # Automated system notifications
    "personal",         # Personal, non-business
    "spam",             # Spam/unwanted
    "other",            # Doesn't fit above
]


class ClassificationResult:
    def __init__(
        self,
        category: str,
        priority: str,
        summary: str,
        action_needed: bool,
    ):
        self.category = category
        self.priority = priority
        self.summary = summary
        self.action_needed = action_needed


async def classify_email(
    from_address: str,
    to_addresses: list[str],
    subject: str,
    body_text: str | None,
    snippet: str | None = None,
) -> ClassificationResult:
    """Classify an email's category, priority, and required action.

    Returns ClassificationResult with all fields populated.
    Falls back to safe defaults on failure.
    """
    # Truncate body to save tokens — first 1000 chars is enough for classification
    body = (body_text or snippet or "")[:1000]

    prompt = f"""Classify this email. Respond in exactly this format (no markdown):
category: <one of: {', '.join(CATEGORIES)}>
priority: <one of: urgent, high, normal, low>
summary: <one sentence summary, max 100 chars>
action_needed: <true or false>

Email:
From: {from_address}
To: {', '.join(to_addresses[:3])}
Subject: {subject}
Body: {body}"""

    try:
        api_key = _resolve_api_key()
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY unavailable to email-agent"
            )
        client = anthropic.AsyncAnthropic(api_key=api_key)
        response = await client.messages.create(
            model=settings.model_haiku,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        return _parse_classification(text)
    except Exception as e:
        logger.error("Email classification failed: %s", e)
        return ClassificationResult(
            category="other",
            priority="normal",
            summary="Classification unavailable",
            action_needed=False,
        )


def _parse_classification(text: str) -> ClassificationResult:
    """Parse Claude's structured response."""
    lines = {}
    for line in text.strip().split("\n"):
        if ":" in line:
            key, _, val = line.partition(":")
            lines[key.strip().lower()] = val.strip()

    category = lines.get("category", "other").lower()
    if category not in CATEGORIES:
        category = "other"

    priority = lines.get("priority", "normal").lower()
    if priority not in ("urgent", "high", "normal", "low"):
        priority = "normal"

    summary = lines.get("summary", "No summary")[:500]

    action_str = lines.get("action_needed", "false").lower()
    action_needed = action_str in ("true", "yes", "1")

    return ClassificationResult(
        category=category,
        priority=priority,
        summary=summary,
        action_needed=action_needed,
    )
