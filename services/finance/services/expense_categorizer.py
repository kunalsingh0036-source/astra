"""AI expense categorization — uses Claude Haiku to classify expenses.

Why Claude Haiku (not rules-based):
- Vendors don't always map cleanly to categories (e.g., "Amazon" could be office supplies,
  software, or inventory)
- Natural language descriptions need semantic understanding
- Haiku is fast (~200ms) and cheap ($1/MTok) — perfect for high-volume classification

Why not a local ML model:
- Would need training data we don't have yet
- Claude Haiku's zero-shot classification is good enough
- Can switch to fine-tuned model later when we have labeled data

Categories follow Indian SME accounting standards (aligned with Tally/bookkeeper-agent).
"""

import anthropic
from pydantic import BaseModel

from finance.config import settings

# Standard expense categories for Indian SMEs
CATEGORIES = [
    "advertising",
    "bank_charges",
    "consulting",
    "depreciation",
    "electricity",
    "employee_benefits",
    "food_beverage",
    "fuel",
    "insurance",
    "internet_telecom",
    "legal",
    "maintenance",
    "office_supplies",
    "packaging",
    "payroll",
    "printing",
    "raw_materials",
    "rent",
    "repairs",
    "shipping",
    "software",
    "subscriptions",
    "taxes_duties",
    "tools_equipment",
    "travel",
    "utilities",
    "vehicle",
    "other",
]


class CategorizationResult(BaseModel):
    category: str
    subcategory: str | None = None
    confidence: float
    reasoning: str


async def categorize_expense(
    vendor_name: str,
    amount: float,
    description: str | None = None,
) -> CategorizationResult:
    """Classify an expense into a category using Claude Haiku.

    Returns category, optional subcategory, confidence score, and reasoning.
    Falls back to 'other' with low confidence if AI call fails.
    """
    prompt = f"""Categorize this business expense into exactly ONE category from the list below.

Expense details:
- Vendor: {vendor_name}
- Amount: ₹{amount:,.2f}
- Description: {description or 'N/A'}

Categories: {', '.join(CATEGORIES)}

Respond in this exact format (no markdown, no extra text):
category: <one category from the list>
subcategory: <optional subcategory or "none">
confidence: <0.0 to 1.0>
reasoning: <one sentence explaining why>"""

    try:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        response = await client.messages.create(
            model=settings.model_haiku,
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        return _parse_response(text)
    except Exception:
        return CategorizationResult(
            category="other",
            subcategory=None,
            confidence=0.0,
            reasoning="AI categorization failed — defaulting to 'other'",
        )


def _parse_response(text: str) -> CategorizationResult:
    """Parse Claude's structured text response into a CategorizationResult."""
    lines = {}
    for line in text.strip().split("\n"):
        if ":" in line:
            key, _, val = line.partition(":")
            lines[key.strip().lower()] = val.strip()

    category = lines.get("category", "other").lower().strip()
    if category not in CATEGORIES:
        category = "other"

    subcategory = lines.get("subcategory")
    if subcategory and subcategory.lower() in ("none", "n/a", ""):
        subcategory = None

    try:
        confidence = float(lines.get("confidence", "0.5"))
        confidence = max(0.0, min(1.0, confidence))
    except ValueError:
        confidence = 0.5

    reasoning = lines.get("reasoning", "No reasoning provided")

    return CategorizationResult(
        category=category,
        subcategory=subcategory,
        confidence=confidence,
        reasoning=reasoning,
    )
