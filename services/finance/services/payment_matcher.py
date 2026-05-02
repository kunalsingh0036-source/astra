"""Payment-to-invoice matching engine.

Why algorithm-based (not AI):
- Matching is mostly deterministic: amounts, dates, and references
- Rules engine is fast, free, and auditable
- AI would be overkill for structured data matching

Matching strategy (in priority order):
1. Exact reference match — payment.reference_number matches invoice.invoice_number
2. Amount + counterparty match — same amount, same party, within date window
3. Partial amount match — payment amount matches invoice balance_due
"""

import uuid
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finance.models.invoice import Invoice, InvoiceStatus
from finance.models.payment import Payment, PaymentStatus


class MatchResult:
    def __init__(
        self,
        payment_id: uuid.UUID,
        invoice_id: uuid.UUID,
        confidence: float,
        match_type: str,
    ):
        self.payment_id = payment_id
        self.invoice_id = invoice_id
        self.confidence = confidence
        self.match_type = match_type

    def __repr__(self):
        return f"<Match payment={self.payment_id} → invoice={self.invoice_id} ({self.match_type}, {self.confidence:.0%})>"


async def match_payment_to_invoice(
    payment: Payment,
    session: AsyncSession,
    date_window_days: int = 30,
) -> MatchResult | None:
    """Find the best invoice match for an unlinked payment.

    Returns the best match or None if no confident match found.
    """
    if payment.invoice_id:
        return None  # Already matched

    candidates = await _get_candidate_invoices(payment, session, date_window_days)

    if not candidates:
        return None

    best_match = None
    best_score = 0.0

    for invoice in candidates:
        score, match_type = _score_match(payment, invoice)
        if score > best_score:
            best_score = score
            best_match = MatchResult(
                payment_id=payment.id,
                invoice_id=invoice.id,
                confidence=score,
                match_type=match_type,
            )

    # Only return matches with reasonable confidence
    if best_match and best_match.confidence >= 0.6:
        return best_match
    return None


async def auto_match_payments(
    business_id: uuid.UUID,
    session: AsyncSession,
    apply: bool = False,
) -> list[MatchResult]:
    """Match all unlinked confirmed payments for a business.

    If apply=True, updates payment.invoice_id and invoice.payment_received.
    """
    q = select(Payment).where(
        Payment.business_id == business_id,
        Payment.invoice_id.is_(None),
        Payment.status == PaymentStatus.CONFIRMED,
    )
    result = await session.execute(q)
    unlinked = result.scalars().all()

    matches = []
    for payment in unlinked:
        match = await match_payment_to_invoice(payment, session)
        if match:
            matches.append(match)
            if apply:
                await _apply_match(payment, match, session)

    if apply:
        await session.commit()

    return matches


async def _get_candidate_invoices(
    payment: Payment,
    session: AsyncSession,
    date_window_days: int,
) -> list[Invoice]:
    """Get unpaid invoices that could match this payment."""
    q = select(Invoice).where(
        Invoice.business_id == payment.business_id,
        Invoice.status.notin_([InvoiceStatus.PAID, InvoiceStatus.CANCELLED]),
        Invoice.issue_date >= payment.payment_date - timedelta(days=date_window_days),
        Invoice.issue_date <= payment.payment_date + timedelta(days=date_window_days),
    )
    result = await session.execute(q)
    return list(result.scalars().all())


def _score_match(payment: Payment, invoice: Invoice) -> tuple[float, str]:
    """Score how well a payment matches an invoice. Returns (score, match_type)."""

    # 1. Exact reference match
    if (
        payment.reference_number
        and payment.reference_number.strip().upper()
        == invoice.invoice_number.strip().upper()
    ):
        return (0.99, "reference_exact")

    score = 0.0
    match_type = "heuristic"

    # 2. Amount matching
    balance = invoice.total_amount - invoice.payment_received
    if payment.amount == balance:
        score += 0.5
        match_type = "amount_exact"
    elif payment.amount == invoice.total_amount:
        score += 0.45
        match_type = "amount_total"
    elif abs(payment.amount - balance) <= Decimal("1.00"):
        score += 0.35  # Rounding difference
        match_type = "amount_close"

    # 3. Counterparty match
    if (
        payment.counterparty_name
        and invoice.counterparty_name
        and payment.counterparty_name.strip().lower()
        == invoice.counterparty_name.strip().lower()
    ):
        score += 0.35

    # 4. Date proximity bonus
    days_diff = abs((payment.payment_date - invoice.due_date).days)
    if days_diff <= 3:
        score += 0.1
    elif days_diff <= 7:
        score += 0.05

    return (min(score, 1.0), match_type)


async def _apply_match(
    payment: Payment,
    match: MatchResult,
    session: AsyncSession,
) -> None:
    """Link payment to invoice and update payment_received."""
    invoice = await session.get(Invoice, match.invoice_id)
    if not invoice:
        return

    payment.invoice_id = match.invoice_id
    invoice.payment_received += payment.amount

    if invoice.payment_received >= invoice.total_amount:
        invoice.status = InvoiceStatus.PAID
    elif invoice.payment_received > Decimal("0.00"):
        invoice.status = InvoiceStatus.PARTIALLY_PAID
