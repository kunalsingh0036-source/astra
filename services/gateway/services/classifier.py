"""
AI reply classifier using Claude Haiku.

Classifies inbound WhatsApp messages to route them correctly.
Uses regex for obvious cases (opt-out, greetings) and Haiku for ambiguous ones.
"""

import logging
import re
from dataclasses import dataclass

import anthropic

from gateway.config import settings

logger = logging.getLogger(__name__)


@dataclass
class Classification:
    label: str
    confidence: float
    method: str  # "regex" or "ai"


# Fast regex patterns for obvious cases
OPT_OUT_PATTERNS = re.compile(
    r"\b(stop|unsubscribe|opt.?out|remove me|don.?t (message|contact|text))\b",
    re.IGNORECASE,
)
GREETING_PATTERNS = re.compile(
    r"^(hi|hello|hey|namaste|hola|good\s?(morning|evening|afternoon))[\s!.]*$",
    re.IGNORECASE,
)

CLASSIFICATION_PROMPT = """Classify this WhatsApp message into exactly ONE category:

- interested: Shows interest in the product/service, asks for details, pricing, demo
- not_interested: Explicitly declines, says no, not looking, not relevant
- question: Asks a question about the business, service, or offer
- complaint: Expresses dissatisfaction, reports a problem
- opt_out: Wants to stop receiving messages
- greeting: Simple hello/hi with no other intent
- scheduling: Wants to book a meeting, call, or appointment
- spam: Irrelevant, automated, or promotional content
- other: Doesn't fit any above category

Message: "{message}"

Recent conversation context (last messages):
{context}

Respond with ONLY the category label, nothing else."""


async def classify_message(
    message_text: str,
    conversation_context: list[str] | None = None,
) -> Classification:
    """Classify an inbound WhatsApp message.

    Tries fast regex first, falls back to Claude Haiku for ambiguous cases.

    Args:
        message_text: The inbound message text
        conversation_context: Recent messages for context (optional)

    Returns:
        Classification with label, confidence, and method
    """
    text = message_text.strip()

    # Fast path: regex for obvious cases
    if OPT_OUT_PATTERNS.search(text):
        return Classification(label="opt_out", confidence=0.95, method="regex")

    if GREETING_PATTERNS.match(text):
        return Classification(label="greeting", confidence=0.90, method="regex")

    # AI classification for everything else
    if not settings.anthropic_api_key:
        return Classification(label="other", confidence=0.5, method="fallback")

    context_str = "\n".join(conversation_context[-5:]) if conversation_context else "No prior context"

    try:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        response = client.messages.create(
            model=settings.model_haiku,
            max_tokens=20,
            messages=[
                {
                    "role": "user",
                    "content": CLASSIFICATION_PROMPT.format(
                        message=text[:500],
                        context=context_str,
                    ),
                }
            ],
        )
        label = response.content[0].text.strip().lower()

        valid_labels = {
            "interested", "not_interested", "question", "complaint",
            "opt_out", "greeting", "scheduling", "spam", "other",
        }
        if label not in valid_labels:
            label = "other"

        return Classification(label=label, confidence=0.85, method="ai")

    except Exception as e:
        logger.error(f"Classification failed: {e}")
        return Classification(label="other", confidence=0.5, method="fallback")
