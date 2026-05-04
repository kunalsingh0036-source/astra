"""
SSE event formatting — re-export from astra.runtime.event_emitter.

Moved upstream during the lean-runtime migration (Phase 2) so both
the legacy SDK runner and the new lean agent loop emit identical
SSE frames from a single source. Existing imports
(`from stream.events import session, thought, ...`) keep working
through this re-export shim.
"""

from astra.runtime.event_emitter import (  # noqa: F401
    artifact,
    done,
    error,
    heartbeat,
    session,
    text_delta,
    thought,
    tool_call,
    tool_result,
)
