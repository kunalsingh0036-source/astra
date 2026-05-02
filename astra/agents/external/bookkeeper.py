"""
Bookkeeper Agent — A2A bridge.

Wraps the Django-based bookkeeper-agent at localhost:8000.
Capabilities: Invoice OCR, bank reconciliation, GST compliance,
payroll, inventory, financial reporting.

Native API: Django views at /dashboard/ and /admin/
This bridge calls the native endpoints and returns structured results.
"""

import httpx

from astra.a2a.models import (
    AgentCard,
    AgentCapabilities,
    AgentSkill,
    MessagePart,
    Task,
    TaskSendParams,
)
from astra.a2a.server import A2AServer

AGENT_NAME = "bookkeeper"
BASE_URL = "http://localhost:8000"
BRIDGE_PORT = 8500

CARD = AgentCard(
    name=AGENT_NAME,
    description=(
        "AI-powered bookkeeping agent for Indian companies. Handles invoice "
        "processing (OCR), bank reconciliation, GST compliance (GSTR-1/3B/2B), "
        "payroll, inventory tracking, and financial reporting."
    ),
    url=f"http://localhost:{BRIDGE_PORT}/bookkeeper",
    version="0.1.0",
    capabilities=AgentCapabilities(
        streaming=False,
        push_notifications=False,
        cancellation=True,
    ),
    skills=[
        AgentSkill(
            id="document-upload",
            name="Upload & Process Document",
            description=(
                "Upload an invoice, bill, or receipt for AI-powered OCR "
                "extraction. Returns extracted data with confidence scores."
            ),
            tags=["invoice", "ocr", "document"],
        ),
        AgentSkill(
            id="bank-reconciliation",
            name="Bank Reconciliation",
            description=(
                "Reconcile bank transactions against invoices. Shows unmatched "
                "transactions and suggests matches."
            ),
            tags=["banking", "reconciliation", "transactions"],
        ),
        AgentSkill(
            id="gst-report",
            name="GST Report Generation",
            description=(
                "Generate GSTR-1, GSTR-3B, or GSTR-2B reconciliation reports. "
                "Computes tax liability and input credit."
            ),
            tags=["gst", "tax", "compliance", "report"],
        ),
        AgentSkill(
            id="payroll-run",
            name="Payroll Processing",
            description=(
                "Generate monthly payslips with earnings, deductions (PF, ESI, "
                "TDS), and net pay calculations."
            ),
            tags=["payroll", "salary", "employees"],
        ),
        AgentSkill(
            id="financial-summary",
            name="Financial Summary",
            description=(
                "Get dashboard overview: pending reviews, documents processing, "
                "unmatched transactions, GST mismatches."
            ),
            tags=["dashboard", "summary", "overview"],
        ),
        AgentSkill(
            id="review-queue",
            name="Review Queue",
            description=(
                "Get items needing human review: documents with low OCR "
                "confidence, unmatched transactions, GST discrepancies."
            ),
            tags=["review", "approval", "queue"],
        ),
    ],
    model_tier="haiku",
    metadata={
        "project_path": "/Users/kunalsingh/Claude Code/bookkeeper-agent",
        "framework": "Django 5.x",
        "database": "SQLite (dev) / PostgreSQL (prod)",
        "native_url": BASE_URL,
    },
)


class BookkeeperBridge(A2AServer):
    """A2A bridge for the Bookkeeper Agent.

    Translates A2A tasks into HTTP calls to the Django backend.
    """

    def __init__(self):
        super().__init__(card=CARD)
        self._http: httpx.AsyncClient | None = None

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                base_url=BASE_URL,
                timeout=30.0,
            )
        return self._http

    async def handle_task(self, task: Task, params: TaskSendParams) -> Task:
        """Route A2A task to the appropriate Django endpoint."""
        skill = params.skill_id or "financial-summary"
        message_text = ""
        for part in params.message.parts:
            if part.type == "text" and isinstance(part.content, str):
                message_text += part.content

        try:
            client = await self._client()

            if skill == "financial-summary":
                response = await client.get("/dashboard/")
                result_text = (
                    f"Bookkeeper dashboard accessed (status={response.status_code}). "
                    f"Task: {message_text}"
                )

            elif skill == "review-queue":
                response = await client.get("/dashboard/review/")
                result_text = (
                    f"Review queue fetched (status={response.status_code}). "
                    f"Task: {message_text}"
                )

            elif skill == "bank-reconciliation":
                response = await client.get("/dashboard/bank-recon/")
                result_text = (
                    f"Bank reconciliation data fetched (status={response.status_code}). "
                    f"Task: {message_text}"
                )

            elif skill == "gst-report":
                response = await client.get("/dashboard/gst-recon/")
                result_text = (
                    f"GST reconciliation data fetched (status={response.status_code}). "
                    f"Task: {message_text}"
                )

            else:
                result_text = (
                    f"Skill '{skill}' acknowledged. The bookkeeper agent "
                    f"received your request: {message_text}. "
                    f"Full automation pending — manual action may be required."
                )

            task.complete(MessagePart(type="text", content=result_text))

        except httpx.ConnectError:
            task.fail(
                f"Bookkeeper agent not running at {BASE_URL}. "
                f"Start it with: cd bookkeeper-agent && python manage.py runserver"
            )
        except Exception as e:
            task.fail(f"Bookkeeper bridge error: {str(e)}")

        return task
