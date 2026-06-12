"""
Finance Agent — A2A bridge.

Cross-business financial intelligence: invoices, payments, expenses,
cash flow forecasting, AI categorization, payment matching, and alerts.

Native API: FastAPI at /api/v1/
Port: 8004
"""

import json
import httpx

from astra.a2a.models import (
    AgentCard,
    AgentCapabilities,
    AgentSkill,
    MessagePart,
    Task,
    TaskSendParams,
    TaskState,
)
from astra.a2a.server import A2AServer

AGENT_NAME = "finance-agent"
import os as _os

# Env-first; cloud default. The laptop-era localhost target was the
# reason every A2A task failed after the Railway migration — the
# bridge ran (eventually) but pointed at services that no longer
# listen on this machine.
BASE_URL = _os.environ.get("FINANCE_URL", "").strip() or "http://finance.railway.internal:8080"
BRIDGE_PORT = 8500

CARD = AgentCard(
    name=AGENT_NAME,
    description=(
        "Cross-business financial intelligence agent. Tracks invoices, payments, "
        "expenses, bank accounts, and cash flow across all of Kunal's businesses. "
        "AI-powered expense categorization, automatic payment-to-invoice matching, "
        "cash flow forecasting, anomaly detection, and financial alerts."
    ),
    url=f"{_os.environ.get('A2A_BRIDGE_BASE', '').strip() or 'http://bridge.railway.internal:8500'}/finance",
    version="0.1.0",
    capabilities=AgentCapabilities(
        streaming=False,
        push_notifications=False,
        cancellation=False,
    ),
    skills=[
        AgentSkill(
            id="dashboard",
            name="Financial Dashboard",
            description=(
                "Get a cross-business financial snapshot: invoice summary, "
                "expense summary, cash flow, bank balances, and recent alerts. "
                "Optionally filter by business_id."
            ),
        ),
        AgentSkill(
            id="list-invoices",
            name="List Invoices",
            description=(
                "List invoices with optional filters: business_id, status "
                "(draft/sent/partially_paid/paid/overdue/cancelled), type (receivable/payable)."
            ),
        ),
        AgentSkill(
            id="create-invoice",
            name="Create Invoice",
            description=(
                "Create a new invoice. Required: business_id, invoice_number, type, "
                "counterparty_name, amount, total_amount, issue_date, due_date."
            ),
        ),
        AgentSkill(
            id="list-expenses",
            name="List Expenses",
            description="List expenses with optional business_id and category filters.",
        ),
        AgentSkill(
            id="categorize-expense",
            name="AI Expense Categorization",
            description=(
                "Classify an expense using Claude Haiku. Provide vendor_name, "
                "amount, and optional description. Returns category, subcategory, "
                "confidence score, and reasoning."
            ),
        ),
        AgentSkill(
            id="match-payments",
            name="Auto-Match Payments",
            description=(
                "Automatically match unlinked payments to invoices for a business. "
                "Uses reference matching, amount matching, and counterparty matching. "
                "Set apply=true to save matches."
            ),
        ),
        AgentSkill(
            id="scan-alerts",
            name="Scan Financial Alerts",
            description=(
                "Run all alert checks for a business: overdue invoices, low cash, "
                "failed payments, unusual expenses."
            ),
        ),
        AgentSkill(
            id="cash-flow-summary",
            name="Cash Flow Summary",
            description=(
                "Get cash flow summary: current balance, 30-day inflow/outflow/net, "
                "and AI forecasts at 30/60/90 days."
            ),
        ),
    ],
    model_tier="haiku",
)


class FinanceBridge(A2AServer):
    """A2A bridge that routes tasks to the Finance Agent REST API."""

    def __init__(self):
        super().__init__(card=CARD)

    async def handle_task(self, task: Task, params: TaskSendParams) -> Task:
        skill_id = params.skill_id
        payload = {}
        for part in params.message.parts:
            if part.type == "json" and isinstance(part.content, dict):
                payload = part.content
            elif part.type == "text" and isinstance(part.content, str):
                try:
                    payload = json.loads(part.content)
                except json.JSONDecodeError:
                    payload = {"query": part.content}

        try:
            async with httpx.AsyncClient(base_url=BASE_URL, timeout=30, headers={"x-astra-secret": _os.environ.get("AGENT_SHARED_SECRET", "").strip()}) as client:
                result = await self._route(client, skill_id, payload)

            task.state = TaskState.COMPLETED
            task.result = MessagePart(
                type="text",
                content=json.dumps(result, indent=2, default=str),
            )
        except Exception as e:
            task.state = TaskState.FAILED
            task.error = str(e)

        return task

    async def _route(self, client: httpx.AsyncClient, skill_id: str | None, payload: dict) -> dict:
        """Route A2A skill to the appropriate Finance Agent endpoint."""

        if skill_id == "dashboard":
            params = {}
            if "business_id" in payload:
                params["business_id"] = payload["business_id"]
            r = await client.get("/api/v1/dashboard/", params=params)
            r.raise_for_status()
            return r.json()

        elif skill_id == "list-invoices":
            params = {k: v for k, v in payload.items() if k in ("business_id", "status", "type", "limit", "offset")}
            r = await client.get("/api/v1/invoices/", params=params)
            r.raise_for_status()
            return {"invoices": r.json(), "count": len(r.json())}

        elif skill_id == "create-invoice":
            r = await client.post("/api/v1/invoices/", json=payload)
            r.raise_for_status()
            return r.json()

        elif skill_id == "list-expenses":
            params = {k: v for k, v in payload.items() if k in ("business_id", "category", "limit", "offset")}
            r = await client.get("/api/v1/expenses/", params=params)
            r.raise_for_status()
            return {"expenses": r.json(), "count": len(r.json())}

        elif skill_id == "categorize-expense":
            r = await client.post("/api/v1/ai/categorize", json=payload)
            r.raise_for_status()
            return r.json()

        elif skill_id == "match-payments":
            business_id = payload.get("business_id")
            apply = payload.get("apply", False)
            r = await client.post(f"/api/v1/ai/match-payments/{business_id}", params={"apply": apply})
            r.raise_for_status()
            return r.json()

        elif skill_id == "scan-alerts":
            business_id = payload.get("business_id")
            r = await client.post(f"/api/v1/ai/scan-alerts/{business_id}")
            r.raise_for_status()
            return {"alerts": r.json(), "count": len(r.json())}

        elif skill_id == "cash-flow-summary":
            params = {}
            if "business_id" in payload:
                params["business_id"] = payload["business_id"]
            r = await client.get("/api/v1/cash-flow/summary", params=params)
            r.raise_for_status()
            return r.json()

        else:
            return {"error": f"Unknown skill: {skill_id}", "available_skills": [s.id for s in CARD.skills]}
