"""The model's only physical verbs: submit_intent and poll_status.

Filing a request is not doing a thing. These two tools write a row and
read a row; authority is a Secure Enclave signature produced on the Mac
with a key the cloud has never seen, over a payload the broker
canonicalises and renders itself. Nothing here can grant anything, and
there is no column behind it to set.

THE SURFACE DECISION, MADE EXPLICITLY
-------------------------------------
`submit_intent` is INTERACTIVE-ONLY. This is a decision, not an
omission — doing nothing would have been a decision too, and the wrong
one.

The reasoning: `exec.shell`, `fs.write` and `fs.edit` are already
compiled into the shipped broker catalogue. They are `signedNoStanding`,
so they cannot execute without Kunal's fingerprint — but an intent for
one of them RAISES A TOUCH ID PROMPT on his Mac. Habituation is the
attack software cannot eliminate, and precise control over when a human
is asked is most of it. Today CONTAINMENT §4 means a prompt-injected
WhatsApp turn cannot even NAME local_bash; without this line, A3 would
hand that capability back through a new door.

So: unattended turns (WhatsApp, schedulers, briefings) cannot file
physical intents at all in A3. Widening that is a per-verb decision for
a later phase, made deliberately, with the broker's rate limit in place
first. `poll_status` is available everywhere — reading a status causes
nothing.

WHY BOTH ARE LOW TIER
---------------------
The gate is the broker, not the tier. But `submit_intent` is WRITE, not
READ: it inserts a durable row that causes a human to be interrupted.
The codebase's own rule is that READ means "changes nothing", and a
tool that queues work for a person changes something. `poll_status` is
genuinely READ. Neither label changes behaviour under today's
semi_auto — the honest label matters if the WRITE row is ever
tightened.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from typing import Any

from astra.runtime.tool_registry import ActionTier, register_tool

logger = logging.getLogger(__name__)

# A queue deeper than a human could plausibly approve is a flood, not a
# backlog. Cheap, cloud-side, and it bounds the case where the model
# loops. The broker's own per-hour rate limit is the real control and it
# does NOT exist yet — until it does, signed verbs stay unreachable
# because no body is registered.
MAX_PENDING = 20

# The executor's Ed25519 public key, for verifying receipts. Empty until
# a body is enrolled: poll_status then reports receipts as UNVERIFIED
# rather than claiming a verdict it cannot compute.
EXECUTOR_PUBKEY_HEX = ""


def _err(msg: str) -> dict:
    return {"content": [{"type": "text", "text": msg}], "is_error": True}


def _ok(msg: str) -> dict:
    return {"content": [{"type": "text", "text": msg}]}


async def _only_body() -> tuple[int | None, str]:
    """The single registered body, or a reason there isn't one.

    A3 ships the pipe; a body registers later. Until then this refuses
    with an honest message rather than filing intents nothing will ever
    claim — CHARTER §8: nothing may silently no-op.
    """
    from sqlalchemy import text
    from astra.db.engine import async_session
    async with async_session() as s:
        rows = (await s.execute(text(
            "SELECT id, label FROM bodies WHERE revoked_at IS NULL "
            "ORDER BY id LIMIT 2"
        ))).fetchall()
    if not rows:
        return None, (
            "No body is registered, so there is nothing to carry this out. "
            "The capability broker's transport exists but no Mac has "
            "enrolled with it yet. Tell Kunal what you could not do and "
            "that it needs the broker enrolled — do not retry."
        )
    if len(rows) > 1:
        return None, (
            "More than one body is registered and this tool cannot yet "
            "choose between them. Tell Kunal."
        )
    return int(rows[0][0]), ""


@register_tool(
    name="submit_intent",
    description=(
        "Ask Kunal's Mac to do one physical thing. This FILES A REQUEST "
        "and causes nothing by itself: the broker on his Mac "
        "canonicalises the arguments itself, renders exactly what he "
        "will read, and — for anything that writes, deletes or runs a "
        "command — requires him to physically touch the Touch ID sensor. "
        "You cannot approve it and neither can any part of Astra.\n\n"
        "Returns an intent_id. Then call poll_status ONCE. If it is not "
        "finished, STOP and tell Kunal it is waiting on him — do not "
        "poll in a loop, and do not re-submit. A signed action can take "
        "as long as it takes him to reach the machine, which is often "
        "longer than this whole conversation.\n\n"
        "Verbs: fs.read, fs.glob, fs.grep, web.screenshot, notes.sync "
        "(no fingerprint needed); fs.write, fs.edit, exec.shell "
        "(fingerprint required, every time, no exceptions).\n\n"
        "Put your reason in `why` — it is shown to Kunal. Never put a "
        "key called `reason` inside `args`; the broker refuses the whole "
        "intent if you do."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "verb": {
                "type": "string",
                "description": "one of the catalogue verbs",
            },
            "args": {
                "type": "object",
                "description": (
                    "arguments for the verb. Paths must be absolute. "
                    "Numbers must be integers — a float is refused, "
                    "because one JSON number has several exact values "
                    "and the signature must be unambiguous."
                ),
            },
            "why": {
                "type": "string",
                "description": (
                    "one line, shown to Kunal on the approval prompt. "
                    "Be specific: he is deciding from this."
                ),
            },
        },
        "required": ["verb", "args", "why"],
    },
    tier=ActionTier.WRITE,
    timeout_sec=20,
    namespace="broker",
)
async def submit_intent_impl(args: dict) -> dict:
    verb = (args.get("verb") or "").strip()
    raw = args.get("args")
    why = (args.get("why") or "").strip()

    if not verb:
        return _err("verb is required")
    if not isinstance(raw, dict):
        return _err("args must be an object")
    if not why:
        return _err(
            "why is required — it is the line Kunal reads when deciding"
        )

    body_id, reason = await _only_body()
    if body_id is None:
        return _err(reason)

    from astra.broker import store

    depth = await store.pending_depth(body_id)
    if depth >= MAX_PENDING:
        return _err(
            f"{depth} intents are already pending on Kunal's Mac, which is "
            "more than he could plausibly approve. Nothing was filed. "
            "Tell him the queue is backed up rather than adding to it."
        )

    # A signed verb takes as long as a human takes; an auto verb should
    # be quick. Neither may sit forever — `claimed` is otherwise
    # indistinguishable from `the broker died`.
    ttl = 3600 if verb in {"fs.write", "fs.edit", "exec.shell"} else 300

    try:
        intent_id = await store.submit_intent(
            body_id=body_id, verb=verb, args=raw, why=why,
            ttl_seconds=ttl,
        )
    except store.ArgsRejected as e:
        return _err(str(e))
    except Exception:
        logger.exception("[broker] submit_intent failed")
        return _err(
            "Could not file the intent — the transport is unavailable. "
            "Tell Kunal; do not retry in a loop."
        )

    return _ok(
        f"Filed intent #{intent_id} ({verb}). Nothing has happened yet.\n"
        f"Call poll_status once with intent_id={intent_id}. If it is "
        "still waiting, say so and stop."
    )


@register_tool(
    name="poll_status",
    description=(
        "Check one intent filed with submit_intent. Call this ONCE per "
        "intent per turn. If the status is pending, claimed or "
        "awaiting_human, STOP and tell Kunal it is waiting on him — "
        "polling again in the same turn will not make him faster, and "
        "you will run out of turn before he reaches the machine."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "intent_id": {"type": "integer"},
        },
        "required": ["intent_id"],
    },
    tier=ActionTier.READ,
    timeout_sec=20,
    namespace="broker",
)
async def poll_status_impl(args: dict) -> dict:
    try:
        intent_id = int(args.get("intent_id"))
    except (TypeError, ValueError):
        return _err("intent_id must be an integer")

    from astra.broker import store
    try:
        row = await store.get_intent_status(intent_id)
    except Exception:
        logger.exception("[broker] poll_status failed")
        return _err("Could not read the intent — transport unavailable.")
    if row is None:
        return _err(f"No intent #{intent_id}.")

    status = row["status"]
    lines = [f"intent #{intent_id} ({row['verb']}): {status}"]

    # The display bytes Kunal was actually shown, if any. Surfaced so a
    # human reading the transcript can see what he was asked, rather
    # than what the model believes it asked for.
    if row.get("display_bytes"):
        try:
            shown = bytes(row["display_bytes"]).decode("utf-8")
            lines.append("what Kunal was shown:\n" + shown.rstrip())
        except Exception:
            lines.append("(display bytes present but not decodable)")

    if status in {"pending", "claimed", "awaiting_human", "running"}:
        lines.append(
            "NOT FINISHED. This is waiting on Kunal or on his Mac. Tell "
            "him what is pending and stop — do not poll again this turn."
        )
        return _ok("\n".join(lines))

    if status in {"denied", "failed", "expired"}:
        lines.append(f"reason: {row.get('deny_reason') or row.get('result_note') or '(none given)'}")
        return _ok("\n".join(lines))

    # Terminal success. RECOMPUTE the receipt verdict — never read a
    # stored boolean. The brain is a Postgres superuser, so a
    # `receipt_verified` column would be a value it writes about itself.
    verdict = _verify_receipt(row)
    lines.append(f"receipt: {verdict}")

    result = row.get("result_bytes")
    if result:
        try:
            lines.append("result:\n" + bytes(result).decode("utf-8", "replace")[:4000])
        except Exception:
            lines.append(f"result: {len(result)} bytes (binary)")
    elif row.get("result_note"):
        lines.append(f"note: {row['result_note']}")
    return _ok("\n".join(lines))


def _verify_receipt(row: dict[str, Any]) -> str:
    """Verify the executor's signature over the receipt, here, now.

    Returns a human-readable verdict. Says UNVERIFIED plainly when it
    cannot check — a verdict it cannot compute must never be reported as
    a pass, which is the whole reason there is no stored boolean.
    """
    rb = row.get("receipt_bytes")
    if not rb:
        return "none present"
    rb = bytes(rb)
    if len(rb) != 194:
        return f"MALFORMED ({len(rb)} bytes, expected 194)"
    if not EXECUTOR_PUBKEY_HEX:
        return (
            "UNVERIFIED — no executor public key is pinned in this build, "
            "so the signature cannot be checked. Do not treat the result "
            "as attested."
        )
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )
        pub = Ed25519PublicKey.from_public_bytes(
            bytes.fromhex(EXECUTOR_PUBKEY_HEX))
        pub.verify(rb[130:194], rb[0:130])
    except Exception:
        return "SIGNATURE DID NOT VERIFY — treat this result as forged"

    result = row.get("result_bytes") or b""
    digest = hashlib.sha256(bytes(result)).digest()
    if digest != rb[94:126]:
        return (
            "signature valid but the RESULT DOES NOT MATCH its signed "
            "digest — treat the result as forged"
        )
    return "verified"
