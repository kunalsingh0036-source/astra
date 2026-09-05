"""
Turn context: facts about the CURRENT turn that come from the
runtime, not from the model.

The model composes every tool argument, so a tool that needs a fact
about the turn it runs in can never take that fact from its args.
run_lean_turn publishes such facts here via ContextVars; tool
dispatch runs inside the turn's task tree, so the values propagate
through asyncio.wait_for's task copy automatically.

The one fact carried today is the surface. The inbound prompt used to
be carried too, for a chat-side approval check that Phase A5 deleted
along with the approval tool it guarded (SECURITY-MODEL §1): no tool
resolves approvals any more, so no tool needs the human's text.

Fail-closed contract: every default here is the least-privileged
value, and consumers must treat an unset context as "outside a turn",
never as permission.
"""

from __future__ import annotations

from contextvars import ContextVar

# The surface the current turn runs on ("interactive" | "unattended").
# Set by run_lean_turn. Defaults to "unattended" — least privilege —
# so code paths OUTSIDE a turn (schedulers, jobs, background tasks)
# are treated as unattended, which is what they are.
#
# This exists because the surface split was enforced only in the agent
# loop's dispatch, and TWO production paths reach the Mac bridge
# without going through it: astra/tools/notes_tools.py::_bridge_sync
# (invoked by the 30-minute scheduler job) and
# astra/tools/reply_tools.py::ingest_voice_export. Both imported the
# bridge helpers directly, so local_bash's DESTRUCTIVE tier AND its
# interactive-only restriction were both bypassed — 48 ungated shell
# executions a day. A guard in one dispatch loop is not a guard; it
# belongs at the chokepoint every caller must cross.
current_surface: ContextVar[str] = ContextVar(
    "current_surface", default="unattended"
)
