"""Dashboard aggregate schemas."""

from decimal import Decimal

from pydantic import BaseModel

from finance.schemas.alert import AlertOut
from finance.schemas.cash_flow import CashFlowSummary
from finance.schemas.expense import ExpenseSummary
from finance.schemas.invoice import InvoiceSummary


class DashboardData(BaseModel):
    """Cross-business financial snapshot — the single API call Astra uses."""

    invoice_summary: InvoiceSummary
    expense_summary: ExpenseSummary
    cash_flow: CashFlowSummary
    recent_alerts: list[AlertOut]
    total_bank_balance: Decimal
