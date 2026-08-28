"""
Turn context — facts about the CURRENT turn that come from the
runtime, not from the model.

The model composes every tool argument, so a tool that needs to know
"what did the human actually type" can never trust its args. This
module carries the genuine inbound prompt from run_lean_turn (which
receives it from the HTTP layer, before the model sees anything)
down to tool handlers via a ContextVar. Tool dispatch runs inside
the turn's task tree, so the value propagates through
asyncio.wait_for's task copy automatically.

First consumer: resolve_approval_tool (CONTAINMENT §5) — the model
may only resolve an approval when the human's own message contains
an explicit approve/deny token for that id. Until the capability
broker replaces the approval mechanism entirely (Workstream A), this
moves the release condition from "the model asserts Kunal said yes"
to "Kunal's actual message says yes".

Fail-closed contract: the default is the empty string, and consumers
must REFUSE when they find it — an unset context is "no human
message available", never "assume yes".
"""

from __future__ import annotations

from contextvars import ContextVar

# The raw text of the human's message for the current turn. Set by
# run_lean_turn at turn start; empty outside a turn.
current_user_prompt: ContextVar[str] = ContextVar(
    "current_user_prompt", default=""
)
