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


def _shell_timeout_range() -> str:
    """"1 and 55000", read out of the mirror rather than written here.

    This sentence used to say the executor CLAMPED an over-long
    timeout. It stopped being true when the Mac started refusing an
    out-of-range value at its precheck, and a description that
    promises a clamp teaches the model to send 300000 — which used to
    cost two fingerprints and one of the day's three irreversible
    units before the jail cut the command off mid-way. A number in a
    sentence is a copy; this is the table the tool validates against.
    """
    for k, lo, hi in client.CATALOGUE_BY_NAME["exec.shell"].int_bounds:
        if k == "timeout_ms":
            return f"{lo} and {hi}"
    raise AssertionError("exec.shell declares no range for timeout_ms")


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
    payloads = False
    for v in client.CATALOGUE:
        # The SAME order Render.swift puts on the approval sheet: the
        # filesystem target first, payloads last. A tool description
        # that listed them differently would have the model describing
        # to Kunal a prompt he is not looking at — which is what the
        # inline copy of this rule that used to live here did, ranking
        # correctly and then breaking ties by required-first while the
        # Mac breaks them alphabetically. One rule, in the mirror.
        ordered = client.display_order(v)
        args = ", ".join(
            (k if k in v.required_keys else f"{k}?")
            + ("*" if k in v.blob_keys else "")
            for k in ordered
        ) or "no args"
        payloads = payloads or bool(v.blob_keys)
        if v.name in client.SECOND_CONFIRMATION_VERBS:
            policy = "TWO fingerprints, every time"
        elif v.signed:
            policy = "fingerprint required, every time"
        else:
            policy = "no fingerprint"
        if v.irreversible:
            policy += (f"; 1 of the {client.DAILY_IRREVERSIBLE_MAX} "
                       "irreversible actions a day")
        is_wired = v.name in published.wired if published is not None else v.wired
        wired = ("wired" if is_wired
                 else "NOT wired yet: refused before filing, nobody is asked")
        lines.append(f"- {v.name}({args}): {policy}; {wired}")
    if payloads:
        lines.append(
            f"* a PAYLOAD argument. Kunal's prompt shows it as "
            f"\"<n> bytes, sha256 <8 hex>, first <k> of <n> bytes: "
            f"<escaped>\" instead of in full, so a 1 MiB write is "
            "readable; the signature still covers every byte. Both "
            "numbers are BYTES — a coverage figure counted in "
            "characters would read 50% while hiding all but one byte "
            "of a payload whose second character carries 60,000 "
            "combining marks. It must be a string and it must be "
            "Unicode NFC — a decomposed payload is refused, not "
            "normalised for you, because the bytes written are the "
            "bytes signed."
        )
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
    "is often longer than this whole conversation.\n\n"
    "THE THREE THAT CHANGE THE MACHINE — fs.write, fs.edit, "
    "exec.shell:\n"
    "- Each needs Kunal's fingerprint EVERY time. There is no standing "
    "grant and no way to earn one; the type in the Mac's catalogue has "
    "no such case.\n"
    "- exec.shell needs TWO taps for one command: the second is a "
    "separate signature the EXECUTOR checks for itself, so it is not a "
    "prompt anything in the cloud can skip. Both sheets are shown "
    "before anything runs, and declining the second runs nothing.\n"
    f"- All three are IRREVERSIBLE and share one counter: "
    f"{client.DAILY_IRREVERSIBLE_MAX} irreversible actions per UTC day, "
    "spent on the Mac and never refunded. Budget them: do not burn one "
    "on a file you could have read first, and prefer one fs.write over "
    "three fs.edits.\n"
    "- Scheduled jobs may file only no-fingerprint verbs. Nothing that "
    "runs while Kunal is asleep can raise a prompt, so never promise "
    "that a write will happen on a schedule.\n"
    "- fs.write REPLACES the whole file. fs.edit replaces EXACTLY ONE "
    "occurrence of `old` and is refused if there are none or several, "
    "so include enough surrounding text to be unique. The Mac now "
    "counts the occurrences BEFORE Kunal is asked: a guessed `old` "
    "comes back as `precondition_failed` naming the count, with no "
    "prompt raised and no unit of the day's three spent, so you can "
    "fix it and resubmit. It is still not free — the refused intent "
    "used one of the ten signed slots this hour — so read the file "
    "first (fs.read costs no fingerprint) and send `old` as the bytes "
    "that are actually there.\n"
    "- fs.write REPLACES; it does not merge, and Kunal's prompt does "
    "NOT tell him how many bytes it is about to destroy — it shows the "
    "path and a summary of what you are writing, not what is already "
    "there. So the burden of knowing what the file currently holds is "
    "YOURS: fs.read it first, and reach for fs.edit when you mean to "
    "change part of a file. If the target did not exist when the "
    "intent was checked and something exists at that name by the time "
    "he taps, the Mac refuses it rather than overwrite a file the "
    "sheet never mentioned — a `precondition_failed` after the "
    "fingerprint, with nothing changed and no unit spent.\n"
    f"- {client.budget_timing_note()}\n"
    "- `content`, `old` and `new` are payloads: Kunal's prompt shows "
    "the size in bytes, a sha256 prefix, how many of those bytes are "
    "shown, and up to 120 characters of the start (fewer when they "
    "escape wide — every non-ASCII character becomes a visible "
    "\\uXXXX). The signature still covers every byte, so what runs is "
    "exactly what you sent; what he READS is the summary. Put the "
    "identifying part first if it matters. They must be strings in "
    "Unicode NFC; a decomposed string is refused rather than silently "
    "normalised.\n"
    f"- exec.shell runs in a sandbox: no network, writes only under "
    f"`cwd`, no access to ~/.ssh, ~/.config or any credential store, "
    f"and no git push. `cwd` is REQUIRED and must be inside a compiled "
    f"root. `timeout_ms` must be between "
    f"{_shell_timeout_range()} ms; outside that it is REFUSED here, "
    "before anything is filed and before Kunal is asked, and never "
    "quietly adjusted — the number you send is the number he reads on "
    "the prompt and the number the command gets.\n"
    f"- Kunal's approval sheet is capped at {client.DISPLAY_MAX_CHARS} "
    "characters and NOTHING is shortened to fit: an intent that would "
    "not fit is refused before he sees anything. The path is printed "
    "in full and the payload preview gets what is left, so a very "
    "long path can leave too little room to show any of the payload "
    "and the whole intent is refused for that — before the prompt, so "
    "nothing was filed, nothing was spent, and nobody is waiting. Say "
    "the path was too long and offer to split the work; do not retry "
    "the same intent. ONE LINE of that sheet may also be no wider than "
    f"{client.DISPLAY_MAX_LINE_CHARS} characters, because the dialog "
    "wraps and a wrapped line pushes the lines under it off the "
    "bottom where he cannot see them: a path, a cwd or an "
    "exec.shell `command` longer than about "
    f"{client.display_line_budget('command')} characters is refused for "
    "that reason alone, even when the sheet as a whole would fit. A "
    "payload never trips it. Shorten the command (put the long part in "
    "a script with fs.write first, then run the script). `why` has its own "
    f"fixed share of that sheet ({client.WHY_MAX_CHARS} characters as "
    "the sheet counts them, and non-ASCII is escaped to a visible "
    "\\uXXXX costing up to 10 each), so it can never shrink the "
    "payload preview he has to read: one line, and a longer one is "
    "refused here before anything is filed. The reasoning goes in the "
    "conversation, not in `why`.\n"
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
        taps = (" It needs TWO taps, not one: the second is a separate "
                "confirmation the executor checks for itself."
                if r.verb in client.SECOND_CONFIRMATION_VERBS else "")
        if r.status == "awaiting_human":
            return (
                "NOT FINISHED: waiting on Kunal's fingerprint at the Touch "
                "ID prompt on his Mac." + taps + " Tell him what is pending "
                "and stop; do not poll again this turn. Nothing typed in "
                "chat can stand in for it."
            )
        return (
            "NOT FINISHED: a fingerprint verb the Mac has not yet shown "
            "Kunal (the broker will raise the Touch ID prompt when it "
            "reaches it)." + taps + " Tell him what is pending and stop; do "
            "not poll again this turn."
        )
    return (
        "NOT FINISHED. This is waiting on Kunal or on his Mac. Tell him "
        "what is pending and stop; do not poll again this turn."
    )


def _nothing_happened(verb: str) -> str:
    """What a refused or failed intent did NOT do, named for the verb.

    "denied" alone is where a model invents a consequence — it has said
    a file was "partially written" from a refusal before. The Mac's
    write verbs are atomic (a temp file renamed over the target) and
    the shell verb is not started at all, so a non-success means
    exactly nothing changed, and this says so in those words."""
    if verb == "fs.write":
        return "The file was NOT written; it is byte-for-byte as it was."
    if verb == "fs.edit":
        return "The file was NOT edited; it is byte-for-byte as it was."
    if verb == "exec.shell":
        return "The command did NOT run."
    return ""


def _render_terminal(r: client.IntentResult) -> list[str]:
    """The lines after the status line for a finished intent: the
    reason for anything but success; the RECOMPUTED receipt verdict
    and the result for success. Shared by poll_status and the inline
    return of submit_intent so the two can never disagree."""
    lines: list[str] = []
    spec = client.CATALOGUE_BY_NAME.get(r.verb or "")
    if r.status != "succeeded":
        lines.append(
            f"reason: {r.deny_reason or r.result_note or '(none given)'}"
        )
        nothing = _nothing_happened(r.verb or "")
        if nothing:
            lines.append(nothing)
        if spec is not None and spec.irreversible:
            # Where the refusal happened decides whether a budget unit
            # went with it, and that is knowable only on the Mac. Say
            # what is true from here: it MAY have cost one.
            lines.append(
                "Do not re-file the same intent hoping for a different "
                "answer: it would ask Kunal for another fingerprint, and "
                f"the day allows {client.DAILY_IRREVERSIBLE_MAX} "
                "irreversible actions in total."
            )
        return lines
    # The verdict was RECOMPUTED by the client from the executor's
    # public key, never read from a stored boolean.
    lines.append(f"receipt: {r.receipt_verdict}")
    # PARTIAL is a success with a consequence, and the row status
    # cannot show it: the broker maps partial onto "succeeded". For
    # fs.write it means the file landed and the full-control ACE for
    # Kunal did not — he owns a file in his own repo he cannot write —
    # and for exec.shell that the command ran and its output was
    # withheld. Read from the byte the executor SIGNED, not a column.
    if r.receipt_outcome == "partial":
        lines.append(
            "PARTIAL: the action HAPPENED and one of its guarantees did "
            "not. Read the result below and tell Kunal exactly which "
            "part did not hold — do not report this as a clean success."
        )
    if spec is not None and spec.irreversible:
        lines.append(
            f"This spent 1 of the {client.DAILY_IRREVERSIBLE_MAX} "
            "irreversible actions the Mac allows today."
        )
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
        "and says how many bytes it left out. For a verb that changes "
        "the machine it also says what did NOT happen when the answer "
        "was no, and flags a PARTIAL outcome — the action happened and "
        "one of its guarantees did not — which the status alone cannot "
        "show. Report that difference to Kunal; never round it up to "
        "success."
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
    # The two facts a caller has to know before planning work that
    # changes the machine, stated from the mirror rather than from
    # anyone's memory of them.
    lines.append(
        f"Irreversible budget: {client.DAILY_IRREVERSIBLE_MAX} actions per "
        "UTC day, shared by "
        + ", ".join(sorted(client.IRREVERSIBLE_VERBS))
        + ". Spent on the Mac inside verification and never refunded; how "
        "many are left today is knowable only there, so plan for few. "
        + client.budget_timing_note()
        + " Confirm with fs.read first; it costs nothing."
    )
    lines.append(
        "Second confirmation: "
        + ", ".join(sorted(client.SECOND_CONFIRMATION_VERBS))
        + " needs TWO taps for one action, the second checked by the "
        "executor itself."
    )
    return _ok("\n".join(lines))
