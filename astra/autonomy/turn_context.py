"""
Turn context: facts about the CURRENT turn that come from the
runtime, not from the model.

The model composes every tool argument, so a tool that needs a fact
about the turn it runs in can never take that fact from its args.
run_lean_turn publishes such facts here via ContextVars; tool
dispatch runs inside the turn's task tree, so the values propagate
through asyncio.wait_for's task copy automatically.

Two facts are carried today: the surface, and the turn's own claim
string. The inbound prompt used to be carried too, for a chat-side
approval check that Phase A5 deleted along with the approval tool it
guarded (SECURITY-MODEL §1): no tool resolves approvals any more, so
no tool needs the human's text.

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
# loop's dispatch, and TWO production paths reached the Mac bridge
# (retired in Phase A6) without going through it: the scheduler's
# 30-minute notes sync and astra/tools/reply_tools.py::
# ingest_voice_export. Both imported the bridge helpers directly, so
# the shell tool's DESTRUCTIVE tier AND its interactive-only
# restriction were both bypassed — 48 ungated shell executions a day.
# A guard in one dispatch loop is not a guard; it belongs at the
# chokepoint every caller must cross. Today that chokepoint is
# astra/broker/client.run_intent, which reads `current_turn` below to
# tell a turn from a job.
current_surface: ContextVar[str] = ContextVar(
    "current_surface", default="unattended"
)

# The claim string of the turn that is running: "turn:<turn id>" (or
# "turn:<session id>" when no turn row exists, as in the local CLI).
# Set by run_lean_turn; empty outside a turn, which is what scheduler
# jobs and background tasks are.
#
# astra/broker/client.py reads this to decide the caller class for a
# physical intent: inside a turn any catalogue verb may be filed on any
# channel (WhatsApp turns are owner-gated, see tool_surface.py);
# outside a turn only `auto` verbs may be filed, so a job can never
# raise a Touch ID prompt. That is a habituation control, not a
# security gate: this value is asserted by the cloud about itself and
# is written to intents.session_claim as exactly that, a claim. The
# fingerprint on the Mac remains the gate.
current_turn: ContextVar[str] = ContextVar("current_turn", default="")
