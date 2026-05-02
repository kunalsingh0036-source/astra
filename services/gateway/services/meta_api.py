"""
Meta WhatsApp Cloud API client.

Lifted from HelmTech's WhatsAppService and refactored:
- Shared httpx.AsyncClient with connection pooling (not per-request)
- Proper error handling with typed exceptions
- Retry logic for transient failures
- Phone normalization moved OUT (handled by gateway service layer)
- Structured response models instead of raw dicts
"""

import asyncio
import logging
from dataclasses import dataclass

import httpx

from gateway.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Response types
# ---------------------------------------------------------------------------


@dataclass
class SendResult:
    """Result of a send operation."""
    success: bool
    message_id: str | None = None
    error: str | None = None
    status_code: int | None = None


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class MetaAPIError(Exception):
    """Base Meta API error."""
    def __init__(self, message: str, status_code: int | None = None):
        self.status_code = status_code
        super().__init__(message)


class RateLimitError(MetaAPIError):
    """Meta API rate limit hit."""
    pass


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class MetaAPIClient:
    """WhatsApp Cloud API client with connection pooling and retries.

    Usage:
        client = MetaAPIClient(http_client)
        result = await client.send_text("919876543210", "Hello!")
    """

    MAX_RETRIES = 3
    RETRY_BACKOFF = [1, 3, 5]  # seconds

    def __init__(self, http_client: httpx.AsyncClient | None = None):
        self._client = http_client
        self._owns_client = http_client is None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30.0)
            self._owns_client = True
        return self._client

    async def close(self) -> None:
        if self._owns_client and self._client and not self._client.is_closed:
            await self._client.aclose()

    @property
    def _base_url(self) -> str:
        return f"{settings.meta_base_url}/{settings.whatsapp_phone_number_id}/messages"

    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {settings.whatsapp_access_token}",
            "Content-Type": "application/json",
        }

    def is_configured(self) -> bool:
        return bool(settings.whatsapp_access_token and settings.whatsapp_phone_number_id)

    async def _send(self, payload: dict) -> SendResult:
        """Send a message with retry logic."""
        if not self.is_configured():
            return SendResult(success=False, error="WhatsApp not configured")

        client = await self._get_client()

        for attempt in range(self.MAX_RETRIES):
            try:
                resp = await client.post(
                    self._base_url,
                    json=payload,
                    headers=self._headers,
                )

                if resp.status_code in (200, 201):
                    data = resp.json()
                    msg_id = data.get("messages", [{}])[0].get("id")
                    return SendResult(success=True, message_id=msg_id)

                if resp.status_code == 429:
                    if attempt < self.MAX_RETRIES - 1:
                        logger.warning(f"Rate limited, retry {attempt + 1}")
                        await asyncio.sleep(self.RETRY_BACKOFF[attempt])
                        continue
                    raise RateLimitError("Meta API rate limit exceeded", 429)

                if resp.status_code >= 500 and attempt < self.MAX_RETRIES - 1:
                    logger.warning(f"Meta API 5xx, retry {attempt + 1}")
                    await asyncio.sleep(self.RETRY_BACKOFF[attempt])
                    continue

                return SendResult(
                    success=False,
                    error=resp.text,
                    status_code=resp.status_code,
                )

            except httpx.TimeoutException:
                if attempt < self.MAX_RETRIES - 1:
                    await asyncio.sleep(self.RETRY_BACKOFF[attempt])
                    continue
                return SendResult(success=False, error="Timeout after retries")

        return SendResult(success=False, error="Max retries exceeded")

    async def send_template(
        self,
        phone: str,
        template_name: str,
        language_code: str = "en",
        components: list | None = None,
    ) -> SendResult:
        """Send a pre-approved template message (works outside 24hr window)."""
        payload = {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language_code},
            },
        }
        if components:
            payload["template"]["components"] = components
        return await self._send(payload)

    async def send_text(
        self,
        phone: str,
        body: str,
        preview_url: bool = False,
    ) -> SendResult:
        """Send a free-form text message (only within 24hr session window)."""
        payload = {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "text",
            "text": {
                "body": body[:4096],
                "preview_url": preview_url,
            },
        }
        return await self._send(payload)

    async def send_media(
        self,
        phone: str,
        media_type: str,
        media_url: str,
        caption: str = "",
    ) -> SendResult:
        """Send a media message (image, document, video)."""
        payload = {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": media_type,
            media_type: {
                "link": media_url,
                "caption": caption[:1024] if caption else "",
            },
        }
        return await self._send(payload)

    async def mark_as_read(self, message_id: str) -> bool:
        """Mark an inbound message as read."""
        if not self.is_configured():
            return False

        client = await self._get_client()
        payload = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id,
        }
        resp = await client.post(self._base_url, json=payload, headers=self._headers)
        return resp.status_code == 200

    async def get_templates(self) -> list[dict]:
        """Fetch all templates from Meta API."""
        if not settings.whatsapp_business_account_id:
            return []

        client = await self._get_client()
        url = f"{settings.meta_base_url}/{settings.whatsapp_business_account_id}/message_templates"
        resp = await client.get(url, headers=self._headers)

        if resp.status_code == 200:
            return resp.json().get("data", [])
        return []
