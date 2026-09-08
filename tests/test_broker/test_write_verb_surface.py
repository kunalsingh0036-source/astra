"""What the model is told about the write verbs, and what it may file.

The model never sees the Mac. Everything it knows about fs.write,
fs.edit and exec.shell comes from three strings — the submit_intent
description, the poll_status rendering and section B of the system
prompt — and from the client's refusals. So each of those is a place a
confabulation can be born and then repeated to Kunal with total
confidence, which is the failure this repo has already paid for twice
(the "Mac is asleep" excuse for a cloud jobstore; the fix flow that
described a push as queued).

Three properties:

  TRUE. Every claim in the prompt about policy is asserted against the
  mirror, and the mirror is asserted against the Swift by
  test_write_verbs.py. Nothing here restates a number.

  COMPLETE ABOUT CONSEQUENCE. A refused write says NOTHING changed. A
  PARTIAL one says the action happened and a guarantee did not — the
  case the row status cannot show, because the broker maps partial
  onto "succeeded".

  NO JOB CAN RAISE A PROMPT. A scheduler job runs while Kunal is
  asleep. It may file `auto` verbs only, and that must hold for every
  irreversible verb, not for a list of three names somebody maintains.

No database: the tool functions are exercised directly and the
client's DB lookups are stubbed at the attribute the client reaches
them through.
"""

from __future__ import annotations

import asyncio
import pathlib
import re

import pytest

from astra.broker import client
from astra.core import system_prompt as sp
from astra.runtime.tools import physical

_ASTRA_ROOT = pathlib.Path(__file__).resolve().parents[2]
_BROKER = _ASTRA_ROOT.parent / "astra-broker"
_VERIFY = _BROKER / "Sources/AstraCore/Verify.swift"

requires_broker = pytest.mark.skipif(
    not _VERIFY.is_file(),
    reason=f"astra-broker is not checked out beside astra at {_BROKER}",
)


def _code(p: pathlib.Path) -> str:
    """Swift with line comments stripped — guards read CODE. The
    step markers this file looks for appear in prose too, and a check
    satisfied by the paragraph that DESCRIBES the rule is a check that
    survives the rule being deleted."""
    return re.sub(r"//[^\n]*", "", p.read_text())


def _run(coro):
    return asyncio.run(coro)


def _text(reply: dict) -> str:
    return "\n".join(
        b["text"] for b in reply["content"] if b.get("type") == "text")


# ── no scheduled job can ever raise a fingerprint prompt ──


@pytest.mark.parametrize("verb", sorted(client.IRREVERSIBLE_VERBS))
def test_a_job_outside_a_turn_cannot_file_a_verb_that_needs_a_fingerprint(
    verb, monkeypatch
):
    """Parametrised over the mirror, not over a written list: a fourth
    irreversible verb must inherit this without anyone remembering."""
    async def _boom(*a, **k):
        raise AssertionError("a job's write must be refused before any lookup")

    monkeypatch.setattr(client, "_only_body", _boom)
    monkeypatch.setattr(client.store, "submit_intent", _boom)

    args = {
        "fs.write": {"path": "/private/tmp/f", "content": "x"},
        "fs.edit": {"path": "/private/tmp/f", "old": "a", "new": "b"},
        "exec.shell": {"command": "ls", "cwd": "/private/tmp"},
    }[verb]
    r = _run(client.run_intent(verb, args, why="a scheduled job",
                               actor="scheduler", session_claim=""))
    assert not r.filed
    assert r.refusal_code == "signed_outside_turn", r.deny_reason
    # The refusal names what a job MAY do, so the caller can fall back
    # rather than guess.
    assert all(v in (r.deny_reason or "") for v in sorted(client.AUTO_VERBS))


# ── the tool surface ──────────────────────────────────────


def test_the_submit_description_tells_the_truth_about_the_write_verbs():
    d = physical._SUBMIT_DESCRIPTION
    assert str(client.DAILY_IRREVERSIBLE_MAX) in d
    assert "TWO taps" in d
    assert "irreversible" in d.lower()
    assert "NFC" in d
    assert "REPLACES the whole file" in d
    assert "EXACTLY ONE" in d
    assert str(client.EXEC_SHELL_MAX_TIMEOUT_MS) in d
    assert str(client.DISPLAY_MAX_CHARS) in d
    # Jobs, said where the model reads about filing.
    assert "only no-fingerprint verbs" in d


def test_the_catalogue_text_marks_payloads_and_the_two_tap_verb():
    t = physical._catalogue_text()
    assert "fs.write(path, content*)" in t
    # `old` before `new`: the sheet shows what is destroyed above what
    # replaces it, so a dialog that clips from the bottom clips the
    # additive half. See client.DESTROYED_BLOB_KEYS.
    assert "fs.edit(path, old*, new*)" in t
    assert "exec.shell(cwd, command, timeout_ms?)" in t
    assert "TWO fingerprints" in t
    for v in client.CATALOGUE:
        if v.irreversible:
            line = next(ln for ln in t.splitlines()
                        if ln.startswith(f"- {v.name}("))
            assert f"1 of the {client.DAILY_IRREVERSIBLE_MAX}" in line, line
    assert "sha256" in t and "NFC" in t


def _result(verb: str, status: str, *, outcome: str = "",
            result: bytes | None = None, reason: str | None = None):
    return client.IntentResult(
        intent_id=7, status=status, verb=verb, result_bytes=result,
        deny_reason=reason, receipt_verdict="verified", receipt_outcome=outcome)


def test_a_refused_write_says_nothing_changed():
    """"denied" alone is where a model invents a consequence. The Mac's
    write is atomic and the shell verb is not started, so a refusal
    means the file is byte-for-byte as it was — in those words."""
    lines = physical._render_terminal(
        _result("fs.write", "denied", reason="path_denied: ..."))
    joined = "\n".join(lines)
    assert "NOT written" in joined
    assert "byte-for-byte" in joined
    assert str(client.DAILY_IRREVERSIBLE_MAX) in joined

    joined = "\n".join(physical._render_terminal(
        _result("fs.edit", "failed", reason="executor_error: ...")))
    assert "NOT edited" in joined

    joined = "\n".join(physical._render_terminal(
        _result("exec.shell", "denied", reason="declined: ...")))
    assert "did NOT run" in joined

    # A read is not a write and gets none of this.
    joined = "\n".join(physical._render_terminal(
        _result("fs.read", "denied", reason="path_denied: ...")))
    assert "NOT written" not in joined and "irreversible" not in joined


def test_a_partial_write_is_never_reported_as_a_clean_success():
    """PARTIAL means the file landed and the full-control ACE for Kunal
    did not: he owns a file in his own repo he cannot write. The row
    status says "succeeded" for it, so the outcome byte the executor
    SIGNED is the only thing that can tell the model otherwise."""
    joined = "\n".join(physical._render_terminal(_result(
        "fs.write", "succeeded", outcome="partial",
        result=b"wrote 12 bytes to /private/tmp/f")))
    assert "PARTIAL" in joined
    assert "did not" in joined
    assert "not report this as a clean success" in joined

    joined = "\n".join(physical._render_terminal(_result(
        "fs.write", "succeeded", outcome="ok", result=b"wrote 12 bytes")))
    assert "PARTIAL" not in joined
    assert f"1 of the {client.DAILY_IRREVERSIBLE_MAX}" in joined


def test_an_open_two_tap_intent_says_it_needs_two_taps():
    open_shell = client.IntentResult(
        intent_id=7, status="awaiting_human", verb="exec.shell")
    assert "TWO taps" in physical._render_open(open_shell)
    open_write = client.IntentResult(
        intent_id=8, status="awaiting_human", verb="fs.write")
    assert "TWO taps" not in physical._render_open(open_write)
    assert "fingerprint" in physical._render_open(open_write)


def test_body_status_reports_the_budget_and_the_second_confirmation(monkeypatch):
    async def _none():
        return None, client._BodyRefusal("No body is registered", "no_body")

    monkeypatch.setattr(client, "_only_body", _none)
    out = _text(_run(physical.body_status_impl({})))
    assert f"{client.DAILY_IRREVERSIBLE_MAX} actions per" in out
    for v in sorted(client.IRREVERSIBLE_VERBS):
        assert v in out
    assert "TWO taps" in out


def test_every_surface_is_honest_about_what_a_refusal_costs(monkeypatch):
    """The one place the budget is easy to describe dishonestly.

    This test used to pin the OPPOSITE sentence, and it was right to:
    an fs.edit whose `old` matched zero or several times was checked
    where the FILE is, several steps after verify() had already spent
    the unit, so a guessed `old` cost a real fingerprint and one of
    three daily actions and changed nothing.

    The body now checks that count BEFORE the prompt (Verify step j'
    and the broker's precheck op), so the honest sentence has changed
    and the surfaces must change with it. The PROPERTY being pinned is
    the same one, and it is not "the refusal is expensive": it is that
    every surface tells the truth about which refusals cost a unit and
    which do not. Both errors are dangerous in the same way. A model
    that believes a refusal is free retries an edit three times and
    spends the day; a model that believes a free refusal is expensive
    stops trying and tells Kunal his budget is gone when it is not —
    the confabulation shape this repo has already paid for.

    So: the cheap refusals must be named as cheap, the expensive one
    (a file that changes under the write, after the check passed) must
    be named as expensive, and the retired claim must be gone from
    every surface rather than merely contradicted somewhere else.

    CORRECTED 2026-09-07, and the correction is the same error one rung
    finer. Both surfaces used to say a file that changed between the
    check and the write "DOES spend the unit, because the unit is spent
    on the attempt". Read against Verify.swift that is wrong: steps (j)
    and (j') — the target and its parent must still be the approved
    objects, and the verb's preconditions re-asked against the file as
    it is now — both run BEFORE the counter at (k). A stale file caught
    there costs the fingerprint and no unit. What spends is a failure
    INSIDE the act, after (k). So there are three cases, not two, and
    the middle one was being reported as the expensive one — a model
    reading it would tell Kunal a third of his day was gone after a
    refusal that moved nothing. Every surface now quotes the one
    function (`client.budget_timing_note`) rather than a paraphrase,
    and this test pins all three cases in each of them.
    """
    async def _none():
        return None, client._BodyRefusal("No body is registered", "no_body")

    monkeypatch.setattr(client, "_only_body", _none)
    surfaces = {
        "submit_intent": physical._SUBMIT_DESCRIPTION,
        "body_status": _text(_run(physical.body_status_impl({}))),
        "system prompt": sp.get_system_prompt(),
    }
    note = client.budget_timing_note()
    for where, text in surfaces.items():
        low = text.lower()
        # The refusals that are now free, named as free.
        assert "precondition_failed" in low, where
        assert "before" in low and "no unit" in low, where
        assert "never refunded" in low, where
        # And the advice that is still the cheapest mitigation.
        assert "fs.read" in text or "`fs.read`" in text, where
        # All three cases, from the ONE function, verbatim: a
        # paraphrase per surface is how the two of them drifted into
        # the same wrong claim in the first place.
        assert note in text, (
            f"{where} paraphrases the budget-timing rule instead of quoting "
            "client.budget_timing_note(); two copies of one rule drift"
        )
        # The retired claims must be GONE, not merely contradicted
        # further down: a surface that says both is a surface the
        # model can quote either half of.
        assert "after the fingerprint still spend" not in low, (
            f"{where} still carries the pre-precheck claim; the count is "
            "checked before the prompt now"
        )
        assert "and that does spend the unit" not in low, (
            f"{where} still says a stale file caught after the prompt spends "
            "a unit; Verify checks that at (j)/(j'), before the counter at (k)"
        )

    # And the middle case is stated, in the note itself: the fingerprint
    # is spent and the unit is not.
    assert "still no unit" in note.lower(), note


@requires_broker
def test_the_budget_is_spent_after_the_staleness_checks_not_before():
    """The claim in `budget_timing_note` is about Verify.swift's ORDER,
    so it is read out of Verify.swift.

    Python cannot run the verifier. What it can do is check that the
    steps the note names as happening first are written first: a
    reordering that put the spend above the path and precondition
    checks would make the note a lie without changing a word of it,
    and would re-open exactly the defect (`verb_unavailable`,
    intent #10) that this whole ordering exists to close.
    """
    verify = _code(_VERIFY)
    spend = verify.index("budget.spend(")
    for marker in ("Denial(.pathChanged", "Denial(.preconditionFailed"):
        assert marker in verify, marker
        assert verify.index(marker) < spend, (
            f"{marker} is raised AFTER the budget spend; a refusal the note "
            "calls free would cost one of the day's three"
        )


# ── the system prompt ─────────────────────────────────────


def test_the_prompt_states_the_write_policy_from_the_mirror():
    p = sp.get_system_prompt()
    assert f"{client.DAILY_IRREVERSIBLE_MAX} irreversible actions per UTC day" in p
    assert str(client.DISPLAY_MAX_CHARS) in p
    assert "TWO of them for one action" in p
    assert "no standing grant" in p
    assert "Unicode NFC" in p
    assert "only no-fingerprint verbs" in p
    assert "NOTHING changed" in p
    # And it must not still claim the write verbs are unavailable.
    assert "neither wired in this build" not in p
    assert "while `exec.shell` is unwired" not in p


def test_the_prompt_never_hardcodes_a_policy_number():
    """The template carries placeholders, not digits: a prompt that
    said "3 a day" would keep saying it after the Mac changed."""
    for n in (str(client.DAILY_IRREVERSIBLE_MAX), str(client.DISPLAY_MAX_CHARS)):
        assert f"{n} irreversible actions per UTC day" not in sp._TEMPLATE
    assert "%%IRREVERSIBLE_MAX%%" in sp._TEMPLATE
    assert "%%DISPLAY_MAX%%" in sp._TEMPLATE
