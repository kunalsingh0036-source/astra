"""
Local bridge — server-side queue + auth.

The bridge gives Astra (running on Railway) the ability to operate
on Kunal's Mac through a local daemon. This module exposes the
server-side surface:

  - issue_bridge_token() — mint a credential for a new daemon
  - validate_bridge_token() — auth a polling request
  - queue_call() — Astra tool inserts a pending call, returns id
  - claim_pending_call() — daemon picks up the next pending call
  - finalize_call() — daemon posts the result
  - wait_for_result() — Astra tool blocks until the call resolves
  - is_path_allowed() — server-side mirror of the daemon's allowlist
    check, applied as a defensive double-check before queuing
"""

from astra.runtime.bridge.store import (
    BridgeToken,
    BridgeCall,
    issue_bridge_token,
    revoke_bridge_token,
    validate_bridge_token,
    expand_bridge_allowlist,
    queue_call,
    claim_pending_call,
    finalize_call,
    wait_for_result,
    is_path_allowed,
)

__all__ = [
    "BridgeToken",
    "BridgeCall",
    "issue_bridge_token",
    "revoke_bridge_token",
    "validate_bridge_token",
    "expand_bridge_allowlist",
    "queue_call",
    "claim_pending_call",
    "finalize_call",
    "wait_for_result",
    "is_path_allowed",
]
