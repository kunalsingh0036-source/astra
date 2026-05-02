"""Shares — payloads dropped into Astra from iOS Share Sheet.

The iOS extension POSTs text / URL / image / audio to /api/share on
astra-stream. This module owns:

  * token issue + validation (paired phones only)
  * payload ingestion with file-on-disk storage
  * pipeline: classify via Claude, route to memory / task / meeting
"""

from astra.shares.store import (
    create_token,
    file_share_payload,
    get_share,
    list_shares,
    recent_shares_for_briefing,
    revoke_token,
    search_shares,
    validate_token,
)

__all__ = [
    "create_token",
    "file_share_payload",
    "get_share",
    "list_shares",
    "recent_shares_for_briefing",
    "revoke_token",
    "search_shares",
    "validate_token",
]
