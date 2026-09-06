"""The model's only physical verbs: submit_intent, poll_status, and the
read-only body_status.

Filing a request is not doing a thing. These tools write a row and
read a row through astra/broker/client.py, the one chokepoint every
code caller shares; authority is a Secure Enclave signature produced
on the Mac with a key the cloud has never seen, over a payload the
broker canonicalises and renders itself. Nothing here can grant
anything, and there is no column behind it to set.

THE SURFACE DECISION, MADE EXPLICITLY
-------------------------------------
`submit_intent` is on BOTH surfaces, interactive and unattended. That
is a decision, not an omission: tool_surface.py deliberately leaves it
out of `_EXTRA_INTERACTIVE_ONLY`, and the reasoning is recorded there.

The case for blocking it on unattended turns was habituation: an
intent for a `signedNoStanding` verb (`exec.shell`, `fs.write`,
`fs.edit`) raises a Touch ID prompt on Kunal's Mac, and controlling
WHEN a human is asked is most of that attack. That case assumed a
prompt-injected WhatsApp message could start a turn. It cannot:
services/gateway/api/webhook.py gates the whole chat path on
`is_owner(phone)`, so only a number in ASTRA_OWNER_NUMBERS reaches
the agent loop, and no scheduler starts turns at all. WhatsApp turns
are therefore owner-gated, and blocking the verb there cost the thing
a body is for (asking Astra to do something on the Mac from the phone)
while defending against a path that does not exist.

The habituation control that does exist sits in the client, keyed on
whether a TURN is running rather than on which channel carried it:
scheduler jobs, which run no turn, may file only auto verbs. It is a
control, not a gate; the fingerprint is the gate.

What the channel rule never covered, and still does not: inside an
owner-initiated turn Astra may READ third-party content that tries to
steer it, on the web surface as much as on WhatsApp. The defences for
that are the broker's honest display, the digest bound to what
executes, the fingerprint itself, and the broker's irreversible-action
budget (`IrreversibleBudget` in Verify.swift). The approval for a
physical intent is that Touch ID prompt and nothing else: no chat
message, no WhatsApp reply and no tool on this surface can stand in
for it. `poll_status` is how the outcome is learned; reading a status
causes nothing.

WHY THE TIERS ARE WHAT THEY ARE
-------------------------------
The gate is the broker, not the tier. But `submit_intent` is WRITE, not
READ: it inserts a durable row that causes a human to be interrupted.
The codebase's own rule is that READ means "changes nothing", and a
tool that queues work for a person changes something. `poll_status`
and `body_status` are genuinely READ. Neither label changes behaviour
under today's semi_auto; the honest label matters if the WRITE row is
ever tightened.
"""

from __future__ import annotations

import logging
import time

from astra.broker import client
from astra.runtime.tool_registry import ActionTier, register_tool

logger = logging.getLogger(__name__)

# How much of a result poll_status shows inline. The cut is ANNOUNCED
# (a "## N more bytes not shown" line) and the whole result stays on
# the intent row, so nothing is silently lost; see _render_result.
RESULT_PREVIEW_CHARS = 4000

# How long submit_intent waits on an AUTO verb before handing back the
# id. Derived in astra/broker/client.py from the measured loop latency
# (6-12 s per auto intent on the live body). The bridge this replaced
# was synchronous; without this wait every chat fs.read/fs.glob was a
# two-turn operation that ended with the model telling Kunal something
# was waiting on him when only the Mac was. A SIGNED verb never waits
# here: it resolves when Kunal reaches the sensor, which is usually
# after this whole conversation.
_AUTO_WAIT_SEC = client.AUTO_INTENT_WAIT_SEC

# The tool's registry budget: the wait plus a 20 s margin for the
# client's DB round trips (body, liveness, depth, insert, the final
# read). tests/test_runtime/test_timeout_hierarchy_broker.py pins
# outer >= inner + margin at every level above it (240 s turn cap >=
# this + 60).
_SUBMIT_TIMEOUT_SEC = _AUTO_WAIT_SEC + 20


def _err(msg: str) -> dict:
    return {"content": [{"type": "text", "text": msg}], "is_error": True}


def _ok(msg: str) -> dict:
    return {"content": [{"type": "text", "text": msg}]}


def _catalogue_text(published: client.PublishedCatalogue | None = None) -> str:
    """The verb list, from the mirror, so the description can never
    advertise a verb the body will refuse. The tool description is
    rendered once at import from the mirror; body_status passes the
    broker's publish when this process has one, so its wired column is
    the executor's own word (client.wired_state decides the same way)."""
    lines = []
    for v in client.CATALOGUE:
        ordered = sorted(v.arg_keys, key=lambda k: (k not in v.required_keys, k))
        args = ", ".join(
            k if k in v.required_keys else f"{k}?" for k in ordered
        ) or "no args"
        policy = ("fingerprint required, every time" if v.signed
                  else "no fingerprint")
        is_wired = v.name in published.wired if published is not None else v.wired
        wired = ("wired" if is_wired
                 else "NOT wired yet: refused before filing, nobody is asked")
        lines.append(f"- {v.name}({args}): {policy}; {wired}")
    return "\n".join(lines)


_SUBMIT_DESCRIPTION = (
    "Ask Kunal's Mac to do one physical thing. This FILES A REQUEST "
    "and causes nothing by itself: the broker on his Mac "
    "canonicalises the arguments itself, renders exactly what he "
    "will read, and, for anything that writes, deletes or runs a "
    "command, requires him to physically touch the Touch ID sensor. "
    "You cannot approve it and neither can any part of Astra.\n\n"
    "Two kinds of verb, two behaviours:\n"
    f"- NO-FINGERPRINT verb (fs.read, fs.glob, ...): waits up to "
    f"{_AUTO_WAIT_SEC} s and returns the RESULT in this same call when "
    "the Mac answers (it usually does within seconds). Proceed with it; "
    "do not call poll_status for a result you already have. If the Mac "
    "has not answered by then the call returns the intent_id and says "
    "so: the Mac is slow, busy or asleep, nothing is waiting on Kunal. "
    "STOP, say that, and check poll_status ONCE next turn; do not poll "
    "in a loop and do not re-submit.\n"
    "- FINGERPRINT verb (fs.write, fs.edit, exec.shell): returns the "
    "intent_id at once. Call poll_status ONCE; if it is not finished, "
    "STOP and tell Kunal it is waiting on his fingerprint. A signed "
    "action takes as long as it takes him to reach the machine, which "
    "is often longer than this whole conversation.\n"
    "An intent that resolves AFTER this turn has ended is listed for "
    "you in the <kunal_now> block at the start of your next turn (id, "
    "verb, outcome, age; the last 6 hours), and Kunal gets a push for "
    "it; one that finished inside the turn is not repeated there.\n\n"
    "A refusal BEFORE filing names its cause: offline (laptop closed, "
    "normal), busy (the Mac is up but inside another intent, named), "
    "not wired, arguments the Mac would refuse. Nothing was filed in "
    "those cases and nobody is waiting; say what could not be done.\n\n"
    "Verbs (the catalogue is compiled into the Mac and closed):\n"
    f"{_catalogue_text()}\n\n"
    "fs.read offset and limit are BYTES, not lines (default 256 KiB, "
    "ceiling 640 KiB; page with offset). A read that contains anything "
    "credential-shaped is withheld whole; the refusal names the byte.\n\n"
    "Put your reason in `why`; it is shown to Kunal. Never put a "
    "key called `reason` inside `args`; the broker refuses the whole "
    "intent if you do. Paths must be absolute and inside the compiled "
    "roots (Claude Code, Documents, /private/tmp); widening a root is a "
    "code change and a re-sign, not a chat command."
)


@register_tool(
    name="submit_intent",
    description=_SUBMIT_DESCRIPTION,
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
                    "Numbers must be integers: a float is refused, "
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
    timeout_sec=_SUBMIT_TIMEOUT_SEC,
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
            "why is required: it is the line Kunal reads when deciding"
        )

    # Branch on the CATALOGUE's policy, never on the verb's name: an
    # auto verb is waited on inline, a signed one is handed back at
    # once (Kunal's fingerprint is nowhere near this turn's clock).
    # An unknown verb gets wait 0 and is refused by the client anyway.
    spec = client.CATALOGUE_BY_NAME.get(verb)
    wait = _AUTO_WAIT_SEC if (spec is not None and not spec.signed) else 0

    try:
        r = await client.run_intent(verb, raw, why=why, actor="chat",
                                    wait_sec=wait)
    except Exception:
        logger.exception("[broker] submit_intent failed")
        return _err(
            "Could not file the intent: the transport is unavailable. "
            "Tell Kunal; do not retry in a loop."
        )
    if not r.filed:
        return _err(r.deny_reason or "refused")

    if r.terminal:
        # The Mac answered inside the wait: the whole outcome, in the
        # same shape poll_status renders, so the model proceeds now.
        lines = [f"intent #{r.intent_id} ({r.verb or verb}): {r.status}"]
        lines.extend(_render_terminal(r))
        return _ok("\n".join(lines))

    if wait:
        return _ok(
            f"Filed intent #{r.intent_id} ({verb}); the Mac has not "
            f"answered within {wait} s (status: {r.status}). This is a "
            "no-fingerprint verb, so NOTHING is waiting on Kunal: the Mac "
            "is slow, busy with another intent, or went to sleep. Stop, "
            "say that, and check poll_status once with "
            f"intent_id={r.intent_id} next turn; do not re-submit."
        )
    return _ok(
        f"Filed intent #{r.intent_id} ({verb}). Nothing has happened yet: "
        "it needs Kunal's fingerprint on the Mac.\n"
        f"Call poll_status once with intent_id={r.intent_id}. If it is "
        "still waiting, say so and stop."
    )


# ── Rendering a result page ───────────────────────────────

# UTF-8 byte classes. A page from fs.read is BYTE-addressed, so either
# edge may fall inside a multibyte character; the bytes are text all
# the same, and must never be reported as binary for that reason.
_CONT_LO, _CONT_HI = 0x80, 0xBF


def _utf8_seq_len(lead: int) -> int:
    """Expected length of the sequence a lead byte starts; 0 if the
    byte is not a lead byte (ASCII or continuation)."""
    if 0xC2 <= lead <= 0xDF:
        return 2
    if 0xE0 <= lead <= 0xEF:
        return 3
    if 0xF0 <= lead <= 0xF4:
        return 4
    return 0


def _decode_page(data: bytes) -> tuple[str | None, int, int, str]:
    """Decode one result page allowing for cut edges.

    Returns (text, lead_trimmed, tail_trimmed, why_binary). `text` is
    None when the bytes are not text: a NUL anywhere, or an invalid
    sequence in the INTERIOR (not at an edge); `why_binary` says
    which. Otherwise `lead_trimmed` continuation bytes were dropped
    from the start (the page began mid-character) and `tail_trimmed`
    bytes of an unfinished sequence from the end (the page ended
    mid-character), and the middle decoded strictly.
    """
    if b"\x00" in data:
        return None, 0, 0, "contains NUL bytes"
    n = len(data)
    lead = 0
    while lead < min(3, n) and _CONT_LO <= data[lead] <= _CONT_HI:
        lead += 1
    tail = 0
    # Look back up to 3 bytes for a lead byte whose sequence runs off
    # the end of the page.
    for back in range(1, min(4, n - lead) + 1):
        b = data[n - back]
        if _CONT_LO <= b <= _CONT_HI:
            continue
        need = _utf8_seq_len(b)
        if need and need > back:
            tail = back
        break
    middle = data[lead:n - tail] if tail else data[lead:]
    try:
        text = middle.decode("utf-8")
    except UnicodeDecodeError as e:
        return None, lead, tail, f"invalid UTF-8 at byte {lead + e.start}"
    return text, lead, tail, ""


def _render_result(r: client.IntentResult) -> str:
    """The result text with an ANNOUNCED cut. The full bytes stay on
    the intent row (intents.result_bytes, intent #id) and are what
    code callers get from astra.broker.client.status(id).result_bytes;
    for fs.read the rest is one more intent away, paged by offset.

    A byte-addressed page can start or end inside a multibyte
    character (an emoji in a WhatsApp export, an accented name). The
    first version decoded strictly and called such a page 'binary';
    a 256 KiB page of plain text with an emoji straddling byte 262144
    was reported as not a text file. Now the cut edges are trimmed and
    NAMED, with the offset arithmetic for the next page, and only a
    NUL or an invalid interior sequence is called binary.
    """
    data = r.result_bytes or b""
    decoded, lead, tail, why = _decode_page(data)
    if decoded is None:
        return (
            f"result: {len(data)} bytes (binary, not shown: {why}). The "
            f"bytes are on intent row #{r.intent_id} (intents.result_bytes)."
        )
    notes = []
    if lead:
        notes.append(
            f"page starts inside a multibyte character: {lead} byte(s) "
            "trimmed at the start (the previous page ends with them)"
        )
    if tail:
        notes.append(
            f"page ends inside a multibyte character: {tail} byte(s) "
            f"trimmed at the end; continue from offset = this page's "
            f"offset + {len(data) - tail} so the character is whole"
        )
    shown = decoded[:RESULT_PREVIEW_CHARS]
    text = "result:\n" + shown
    hidden = len(data) - lead - tail - len(shown.encode("utf-8"))
    if hidden > 0:
        text += (
            f"\n## {hidden} more bytes not shown. The full result is on "
            f"intent row #{r.intent_id} (intents.result_bytes); for "
            "fs.read, page the rest with offset."
        )
    for note in notes:
        text += f"\n## {note}."
    return text


def _is_signed(verb: str) -> bool | None:
    """True/False from the catalogue; None for a verb it does not
    hold (a row written by an older build), so the text stays neutral
    rather than guessing which human, if any, is involved."""
    spec = client.CATALOGUE_BY_NAME.get(verb or "")
    return None if spec is None else spec.signed


def _render_open(r: client.IntentResult) -> str:
    """What an unfinished intent is waiting ON, by the verb's policy.
    The first version said 'waiting on Kunal or on his Mac' for every
    open row, so a chat fs.read that the Mac was still executing ended
    with the model telling Kunal something was waiting on him."""
    signed = _is_signed(r.verb)
    if signed is False:
        return (
            "NOT FINISHED: waiting on the Mac, not on Kunal (no-fingerprint "
            "verb; it usually answers within seconds, and submit_intent "
            f"already waited {_AUTO_WAIT_SEC} s). The Mac is slow, busy "
            "with another intent, or asleep. Stop; do not poll again this "
            "turn; check once next turn."
        )
    if signed is True:
        if r.status == "awaiting_human":
            return (
                "NOT FINISHED: waiting on Kunal's fingerprint at the Touch "
                "ID prompt on his Mac. Tell him what is pending and stop; "
                "do not poll again this turn. Nothing typed in chat can "
                "stand in for it."
            )
        return (
            "NOT FINISHED: a fingerprint verb the Mac has not yet shown "
            "Kunal (the broker will raise the Touch ID prompt when it "
            "reaches it). Tell him what is pending and stop; do not poll "
            "again this turn."
        )
    return (
        "NOT FINISHED. This is waiting on Kunal or on his Mac. Tell him "
        "what is pending and stop; do not poll again this turn."
    )


def _render_terminal(r: client.IntentResult) -> list[str]:
    """The lines after the status line for a finished intent: the
    reason for anything but success; the RECOMPUTED receipt verdict
    and the result for success. Shared by poll_status and the inline
    return of submit_intent so the two can never disagree."""
    lines: list[str] = []
    if r.status != "succeeded":
        lines.append(
            f"reason: {r.deny_reason or r.result_note or '(none given)'}"
        )
        return lines
    # The verdict was RECOMPUTED by the client from the executor's
    # public key, never read from a stored boolean.
    lines.append(f"receipt: {r.receipt_verdict}")
    if r.result_bytes:
        lines.append(_render_result(r))
    elif r.result_note:
        lines.append(f"note: {r.result_note}")
    return lines


@register_tool(
    name="poll_status",
    description=(
        "Check one intent filed with submit_intent. Call this ONCE per "
        "intent per turn. If it is still open, the result says what it "
        "is waiting on: a FINGERPRINT verb waits on Kunal at the Touch "
        "ID sensor, so STOP and tell him what is pending (polling again "
        "will not make him faster, and you will run out of turn before "
        "he reaches the machine); a NO-FINGERPRINT verb waits on the "
        "Mac only, usually seconds, and submit_intent already waited "
        f"{_AUTO_WAIT_SEC} s for it, so STOP and check once next turn. "
        "Never poll in a loop. Shows up to 4000 characters of a result "
        "and says how many bytes it left out."
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

    try:
        r = await client.status(intent_id)
    except Exception:
        logger.exception("[broker] poll_status failed")
        return _err("Could not read the intent: transport unavailable.")
    if r is None:
        return _err(f"No intent #{intent_id}.")

    lines = [f"intent #{intent_id} ({r.verb}): {r.status}"]

    # The display bytes Kunal was actually shown, if any. Surfaced so a
    # human reading the transcript can see what he was asked, rather
    # than what the model believes it asked for.
    if r.display:
        lines.append("what Kunal was shown:\n" + r.display)

    if not r.terminal:
        lines.append(_render_open(r))
        return _ok("\n".join(lines))

    lines.extend(_render_terminal(r))
    return _ok("\n".join(lines))


@register_tool(
    name="body_status",
    description=(
        "Whether Kunal's Mac (the body) is polling for work, when it last "
        "finished something, and which catalogue verbs it can perform "
        "in this build. READ; causes nothing. Use it when a submit_intent "
        "was refused as offline, busy or unwired and you need to explain "
        "why, not as a preamble to every request."
    ),
    input_schema={"type": "object", "properties": {}},
    tier=ActionTier.READ,
    timeout_sec=10,
    namespace="broker",
)
async def body_status_impl(args: dict) -> dict:
    lines = []
    body_id = None
    try:
        body_id, reason = await client._only_body()
        if body_id is None:
            lines.append(reason)
        else:
            from astra.broker import store
            live = await store.body_liveness(body_id)
            if live is None:
                lines.append(f"body #{body_id}: no row (revoked?)")
            else:
                if live.poll_age_sec is None:
                    lines.append(
                        f"Mac body '{live.label}': never polled."
                    )
                else:
                    state = ("polling" if live.poll_age_sec
                             <= client.BODY_POLL_WINDOW_SEC else "not polling")
                    lines.append(
                        f"Mac body '{live.label}': {state}, last poll "
                        f"{int(live.poll_age_sec)} s ago"
                        f" (window {client.BODY_POLL_WINDOW_SEC} s)."
                    )
                lines.append(
                    "last completed intent: "
                    + (live.last_completed_at.isoformat(timespec="seconds")
                       if live.last_completed_at else "never")
                )
                # What the broker is inside right now, if anything: a
                # body that is 'not polling' with an open intent is
                # busy, not closed (the serve loop is single-threaded).
                try:
                    open_rows = await store.open_intents(body_id)
                except Exception:
                    open_rows = []
                if open_rows:
                    lines.append(
                        "busy with: " + "; ".join(
                            f"#{o['id']} {o['verb']} {o['status']}"
                            for o in open_rows
                        ) + " (the broker polls again when these resolve)"
                    )
    except Exception:
        logger.exception("[broker] body_status failed")
        lines.append("Could not read the body row: transport unavailable.")

    published = client.published_wired(body_id)
    lines.append("catalogue in this build:")
    lines.append(_catalogue_text(published))
    if published is not None:
        lines.append(
            "wired set: as the broker published it to this process "
            f"{int(time.time() - published.at)} s ago (executor build "
            f"{published.build_cdhash[:12] or 'unknown'})"
        )
    else:
        lines.append(
            "wired set: from the cloud mirror (no publish carrying a wired "
            "flag has reached this process since it started; the mirror "
            "is pinned to the Mac sources by test)"
        )
    lines.append(
        "Receipts: "
        + ("verified against the pinned executor key"
           if (client.settings.executor_pubkey_hex or "").strip()
           else "UNVERIFIED (no executor public key pinned)")
    )
    return _ok("\n".join(lines))
