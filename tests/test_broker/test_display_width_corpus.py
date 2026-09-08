"""The cloud half of the shared ESCAPING corpus.

`Render.safe` on the Mac decides what a string costs on Kunal's
approval sheet: printable ASCII passes through, a newline becomes two
columns, and everything else — every homoglyph, every bidi control,
every emoji — becomes a visible ``\\uXXXX`` or ``\\UXXXXXXXX``. The
cloud holds a SECOND implementation of that rule,
``client.display_width``, and uses it to refuse an over-long ``why``
before anything is filed.

Two implementations of one rule, each with its own tests and its own
examples, is the shape that already cost this system a live outage:
Swift read JSON ``0`` as a boolean and refused ``offset: 0`` while the
cloud accepted it, so the first page of every paged ``fs.read`` failed
for months with both suites green. The examples for THIS rule
therefore live in one place — ``widthCorpusJSON`` in
``astra-broker/Tests/AstraCoreTests/RenderTests.swift`` — and this file
parses them out of it. A vector added on either side is a vector both
sides run.

What is at stake if they drift: ``display_width`` UNDER-counting lets
the cloud file a ``why`` the Mac then refuses as unrenderable, which
is loud and cheap. OVER-counting refuses a legitimate one here, which
is silent — the model simply never gets to explain itself on the sheet
Kunal reads. Neither is caught by any test that only runs on one side.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import re

import pytest

from astra.broker import client, store

_READABLE = "/Users/kunalsingh/Claude Code/astra/README.md"


@pytest.fixture
def no_db(monkeypatch):
    """Nothing here reaches a database, and `submit_intent` records
    rather than files.

    The `why` gate runs before any lookup, so its refusal must be
    provable with every DB entry point poisoned. Body resolution comes
    AFTER it and returns a deterministic `no_body` refusal, which is
    how a test can tell "the `why` was accepted" apart from "the `why`
    was refused" without a live Mac.

    The package conftest already refuses a non-loopback DSN; this
    fixture is the second half of the same rule — these tests never
    open a session at all.
    """
    filed: list = []

    async def _boom(*a, **k):
        raise AssertionError("must not touch the database for this refusal")

    async def _submit(**kw):
        filed.append(kw)
        return 42

    async def _no_body():
        return None, client._BodyRefusal("No body is registered", "no_body")

    monkeypatch.setattr(store, "submit_intent", _submit)
    monkeypatch.setattr(store, "body_liveness", _boom)
    monkeypatch.setattr(store, "pending_depth", _boom)
    monkeypatch.setattr(client, "_only_body", _no_body)
    return filed


_ASTRA_ROOT = pathlib.Path(__file__).resolve().parents[2]
_RENDER_TESTS = (_ASTRA_ROOT.parent / "astra-broker"
                 / "Tests/AstraCoreTests/RenderTests.swift")

requires_broker = pytest.mark.skipif(
    not _RENDER_TESTS.is_file(),
    reason=f"astra-broker is not checked out beside astra at {_RENDER_TESTS}; "
           "the escaping mirror is unpinned in this checkout",
)


def _corpus() -> list[dict]:
    src = _RENDER_TESTS.read_text()
    m = re.search(r'widthCorpusJSON = #"""\n(.*?)\n\s*"""#', src, re.S)
    assert m, (
        "widthCorpusJSON is gone from RenderTests.swift. It is the only "
        "list of examples both implementations of the escaping run; "
        "restore it rather than writing a second one here."
    )
    return json.loads(m.group(1))


def _cases():
    if not _RENDER_TESTS.is_file():
        return [{"s": "", "escaped": "", "w": 0, "why": "skipped"}]
    return _corpus()


@requires_broker
def test_the_corpus_is_present_and_has_not_shrunk():
    cases = _corpus()
    assert len(cases) > 15, "the shared escaping corpus lost vectors"
    # The three that carry the whole point of escaping at all: a
    # homoglyph, a bidi override, and a zero-width character. If one of
    # these is ever dropped, the corpus stops testing the thing the
    # renderer exists for.
    escaped = {c["escaped"] for c in cases}
    for must in ("\\u0433", "\\u202E", "\\u200B"):
        assert must in escaped, f"{must} must stay in the corpus"


@requires_broker
@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["why"][:48])
def test_display_width_agrees_with_the_shared_corpus(case):
    """The number the cloud computes is the number of columns the Mac
    will print. Asserted per vector, with the reason the vector exists
    in the failure message — a width mismatch reads as an arithmetic
    detail and is actually a claim about what Kunal can see."""
    assert client.display_width(case["s"]) == case["w"], case["why"]


@requires_broker
@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["why"][:48])
def test_the_corpus_does_not_contradict_itself(case):
    """`w` is the length of `escaped`. The Swift half asserts that
    `Render.safe` PRODUCES `escaped`; this asserts the width beside it
    is that string's length, so the two halves cannot be reconciled by
    quietly editing the number."""
    assert len(case["escaped"]) == case["w"], case["why"]


@requires_broker
def test_the_why_cap_is_measured_in_escaped_columns_not_characters(no_db):
    """The cap that `display_width` exists for, at its boundary, and
    through the code path that actually enforces it.

    A `why` of one Devanagari character costs six columns on the sheet
    and one emoji ten, so a cap counted in `len(s)` is not a cap on the
    sheet at all: 84 emoji is 840 columns of a 400-column display. That
    is the same mistake the Swift preview budget already paid for once.

    Asserted through `run_intent` rather than on `display_width` alone,
    because the property worth having is that the cap is WIRED to the
    width function. A correct width function the refusal does not
    consult protects nothing.
    """
    n = client.WHY_MAX_CHARS
    assert client.display_width("w" * n) == n
    emoji = "\U0001F600"
    assert client.display_width(emoji * n) == 10 * n > n

    def refusal(why: str):
        return asyncio.run(client.run_intent(
            "fs.read", {"path": _READABLE}, why=why,
            actor="test", session_claim="turn:1"))

    # Exactly the cap passes the `why` gate — it gets all the way to
    # body resolution, which is the next thing that can refuse it. One
    # column over never gets there.
    assert refusal("w" * n).refusal_code == "no_body"
    over = refusal("w" * (n + 1))
    assert over.refusal_code == "args"
    assert str(n) in over.deny_reason and "escaped" in over.deny_reason
    assert no_db == [], "a `why` refusal must not reach the database"

    # Ten columns each, so a `why` a tenth of the cap in CHARACTERS is
    # already over it in columns. This is the property no ASCII-only
    # test can show, and the reason the corpus carries emoji at all.
    short = emoji * (n // 10 + 1)
    assert len(short) < n
    assert refusal(short).refusal_code == "args"
