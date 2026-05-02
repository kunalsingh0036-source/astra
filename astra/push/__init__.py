"""Web Push — lock-screen notifications on iOS PWA + desktop browsers.

Astra sends through this module instead of — or alongside — the macOS
`notifications` module. Subscriptions are stored in push_subscriptions;
a send broadcasts to every active row, prunes dead endpoints, records
the outcome.
"""

from astra.push.sender import broadcast, send_to_subscription

__all__ = ["broadcast", "send_to_subscription"]
