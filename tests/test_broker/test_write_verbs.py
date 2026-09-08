"""The three verbs that change the machine, from the cloud's side.

fs.write, fs.edit and exec.shell went from "in the catalogue, refused
at dispatch" to wired on 2026-09-06. Everything the cloud can get
wrong about them is a claim about the MAC that Python cannot check by
running anything, so these tests do two things and nothing else:

  PIN THE MIRROR to the Swift sources, by parsing them. A mirror that
  drifts does not fail loudly — it files an intent the executor will
  refuse, or refuses one the executor would have run, and both look
  like the Mac misbehaving.

  PIN WHAT THE MODEL IS TOLD. The tool descriptions and the system
  prompt are the only place the model learns that a write costs a
  fingerprint, that exec.shell costs two, that the day holds three
  irreversible actions, and that a refusal means NOTHING changed. A
  prompt that says "3 a day" while the Mac says otherwise is a
  confabulation with a straight face, and the model would repeat it to
  Kunal — so every number in it is asserted to come from the mirror.

Nothing here touches a database. The one number that cannot be read
from a Swift literal — how many irreversible units are left today —
is deliberately not modelled anywhere in the cloud: it lives in the
executor's ratchet, and a cloud copy of it would be a value the brain
could write about itself.
"""

from __future__ import annotations

import dataclasses
import pathlib
import re
import unicodedata

import pytest

from astra.broker import client
from astra.runtime.tools import physical

_ASTRA_ROOT = pathlib.Path(__file__).resolve().parents[2]
_BROKER = _ASTRA_ROOT.parent / "astra-broker"
_CATALOG = _BROKER / "Sources/AstraCore/Catalog.swift"
_CANON = _BROKER / "Sources/AstraCore/Canonicalizer.swift"
_RENDER = _BROKER / "Sources/AstraCore/Render.swift"
_TOKEN = _BROKER / "Sources/AstraCore/Token.swift"
_SHELL = _BROKER / "Sources/AstraCore/ShellVerb.swift"

requires_broker = pytest.mark.skipif(
    not _CATALOG.is_file(),
    reason=f"astra-broker is not checked out beside astra at {_BROKER}; "
           "the write-verb mirror is unpinned in this checkout",
)


def _code(p: pathlib.Path) -> str:
    """Swift with line comments stripped. Guards read CODE: the first
    version of a guard in this repo fired on the comment that
    EXPLAINED the rule, and a guard you silence by deleting the
    documentation is worse than none."""
    return re.sub(r"//[^\n]*", "", p.read_text())


# ── the mirror ────────────────────────────────────────────


WRITE_VERBS = ("fs.write", "fs.edit", "exec.shell")


def test_the_three_verbs_are_wired_with_their_exact_arguments():
    """Written out, not derived. This is the record of what the model
    is allowed to file; a change to any of it should be visible in a
    diff of this test, not only in a diff of a table."""
    expected = {
        "fs.write": (frozenset({"path", "content"}),
                     frozenset({"path", "content"}),
                     frozenset({"content"})),
        "fs.edit": (frozenset({"path", "old", "new"}),
                    frozenset({"path", "old", "new"}),
                    frozenset({"old", "new"})),
        # cwd is REQUIRED: the jail's one writable subtree is the
        # approved cwd, so it is a security-relevant line on Kunal's
        # prompt and there is no honest default.
        "exec.shell": (frozenset({"command", "cwd", "timeout_ms"}),
                       frozenset({"command", "cwd"}),
                       frozenset()),
    }
    for name, (args, required, blobs) in expected.items():
        spec = client.CATALOGUE_BY_NAME[name]
        assert spec.wired, f"{name} must be filable"
        assert spec.signed, f"{name} must need a fingerprint"
        assert spec.policy == "signedNoStanding", f"{name} may never hold a grant"
        assert spec.irreversible, f"{name} must count against the daily budget"
        assert spec.arg_keys == args, name
        assert spec.required_keys == required, name
        assert spec.blob_keys == blobs, name
    assert set(WRITE_VERBS) <= client.WIRED_VERBS
    assert client.IRREVERSIBLE_VERBS == frozenset(WRITE_VERBS), (
        "every irreversible verb shares the one daily counter; a new one "
        "changes what 3 a day means and must be a deliberate edit here"
    )


def test_the_argument_order_told_to_the_model_is_the_sheets_order():
    """One rule for "what order does Kunal read these in", written out
    here as the record of the decision.

    THE DEFECT THIS PINS. The cloud had two other answers before
    `display_order` existed: the tool description ranked correctly and
    then broke ties by required-first, and the system prompt carried a
    hand-typed table per verb that ignored the classes altogether. The
    hand-typed table said `exec.shell(command, cwd, timeout_ms)`. The
    sheet leads with `cwd`, because cwd is the path argument and the
    sheet leads with the thing being acted on — so the model was
    describing to Kunal a prompt he was not looking at, and a
    hand-maintained list is not a mirror in any case.
    """
    assert {v.name: client.display_order(v) for v in client.CATALOGUE} == {
        "fs.read": ("path", "limit", "offset"),
        "fs.glob": ("pattern", "root"),
        "fs.grep": ("path", "include", "pattern"),
        "web.screenshot": ("height", "url", "width"),
        "notes.sync": (),
        "fs.write": ("path", "content"),
        "fs.edit": ("path", "old", "new"),
        "exec.shell": ("cwd", "command", "timeout_ms"),
        "body.probe": ("target",),
    }, ("the filesystem target first, then plain arguments, then what "
        "the verb DESTROYS, then the rest of the content class; "
        "alphabetical inside each class — the sheet's rule, not a "
        "readable-looking one")


def test_the_deletion_is_read_before_the_replacement():
    """fs.edit shows `old` above `new`, and the reason is the dialog.

    The sheet's total size is bounded and its ROW COUNT is not: nobody
    has yet measured what macOS draws for a 400-character, five-line
    `LAContext.localizedReason`. A dialog that clips does it from the
    bottom and says nothing, so whichever argument is last is the one
    at risk of never being read — and under plain alphabetical order
    inside the content class that was `old`, the field naming the text
    about to be destroyed. Ordering costs nothing; the measurement is
    still owed.
    """
    order = client.display_order(client.CATALOGUE_BY_NAME["fs.edit"])
    assert order.index("old") < order.index("new"), order
    assert order[0] == "path", order
    for name, spec in client.CATALOGUE_BY_NAME.items():
        destroyed = client.DESTROYED_BLOB_KEYS.get(name, frozenset())
        assert destroyed <= spec.blob_keys, name
        if len(spec.blob_keys) >= 2:
            assert destroyed, (
                f"{name} carries {sorted(spec.blob_keys)} and names none of "
                "them as the destroyed one, so the sheet orders them "
                "alphabetically and the deletion can be the clipped row")
            assert destroyed < spec.blob_keys, (
                f"{name} marks every payload as destroyed, which orders nothing")
    assert set(client.DESTROYED_BLOB_KEYS) <= set(client.CATALOGUE_BY_NAME)


@requires_broker
def test_no_timeout_this_module_files_can_outlast_the_verbs_runtime_bound():
    """The largest `timeout_ms` the cloud will ever send is strictly
    inside the runtime the body gives exec.shell.

    THE DEFECT BEHIND IT. `max_runtime_ms` used to be a field of the
    signed token, i.e. a number the BROKER wrote, and the executor's
    watchdog waited exactly that long and then reported the action
    abandoned. It cannot kill anything, so a short value did not stop
    the jailed child: the receipt said the command failed while it ran
    to completion in the approved cwd, spending one of the day's three
    irreversible units on an outcome the audit chain recorded
    backwards. It is a compiled per-verb fact now
    (`VerbSpec.maxRuntimeMs`, refused at Verify when the token
    disagrees), and this is the half of the relation the cloud can
    see: file the maximum timeout and the jail's own SIGKILL still
    fires first, so "abandoned" can never be a lie about a command
    that finished.
    """
    from tests.test_broker import test_catalogue_mirror as mirror

    swift = {v["name"]: v for v in mirror._parse_catalog(_CATALOG.read_text())}
    assert swift, "Catalog.swift parse found nothing; the mirror is unpinned"
    shell = swift["exec.shell"]
    assert shell["max_runtime_ms"] > client.EXEC_SHELL_MAX_TIMEOUT_MS, (
        f"exec.shell's compiled runtime bound is {shell['max_runtime_ms']} ms "
        f"and this module will file timeouts up to "
        f"{client.EXEC_SHELL_MAX_TIMEOUT_MS} ms; the watchdog would report an "
        "action abandoned while the child kept running"
    )
    lo, hi = next((lo, hi) for k, lo, hi in
                  client.CATALOGUE_BY_NAME["exec.shell"].int_bounds
                  if k == "timeout_ms")
    assert lo >= 1 and hi == client.EXEC_SHELL_MAX_TIMEOUT_MS, (lo, hi)


@requires_broker
def test_the_destroyed_key_table_is_the_one_compiled_into_the_renderer():
    """Parsed from Render.swift, because a table in two repositories is
    two facts until something compares them.

    The Mac keeps this OUT of VerbSpec deliberately (display order is
    the renderer's job, not the catalogue's) and refuses to boot on a
    verb with two payloads and no declaration
    (`Render.assertOrderWellFormed`). Both halves are checked here: the
    literal, and the boot assertion that makes forgetting it loud.
    """
    render = _code(_RENDER)
    m = re.search(
        r"destroyedBlobKeys:\s*\[String:\s*Set<String>\]\s*=\s*\[(.*?)\]\s*$",
        render, re.S | re.M)
    assert m, "Render.destroyedBlobKeys is gone; DESTROYED_BLOB_KEYS mirrors nothing"
    swift = {
        verb: frozenset(re.findall(r'"([^"]+)"', keys))
        for verb, keys in re.findall(r'"([^"]+)"\s*:\s*\[([^\]]*)\]', m.group(1))
    }
    assert swift == client.DESTROYED_BLOB_KEYS, (swift, client.DESTROYED_BLOB_KEYS)
    assert "assertOrderWellFormed" in render, (
        "the boot assertion that refuses a two-payload verb with no declared "
        "destroyed key is gone, so the table is hand-maintained with no guard")


def test_the_tiebreak_is_alphabetical_and_not_required_first():
    """The half of the rule that no real verb currently distinguishes.

    Every catalogue verb today happens to order the same way under
    "alphabetical" and under "required first, then alphabetical", so
    the divergence that shipped could not be seen from the catalogue.
    A synthetic spec is the only thing that separates them, and without
    it the next verb with an optional argument early in the alphabet
    re-opens the same hole silently.
    """
    spec = dataclasses.replace(
        client.CATALOGUE_BY_NAME["fs.read"],
        name="synthetic",
        arg_keys=frozenset({"aaa", "zzz"}),
        required_keys=frozenset({"zzz"}),
        path_keys=frozenset(),
        int_keys=frozenset(),
    )
    assert client.display_order(spec) == ("aaa", "zzz"), (
        "ties break alphabetically; required-first is a different rule "
        "and the Mac does not use it")


@requires_broker
def test_the_order_rule_is_the_one_compiled_into_the_renderer():
    """Parsed, because Python cannot run Swift. `rank` and the sort in
    `orderedKeys` are two lines and both are load-bearing: the ranks
    decide which line Kunal reads first, and the tiebreak decides the
    rest."""
    render = _code(_RENDER)
    m = re.search(
        r"static func rank\(_ spec: VerbSpec, _ key: String\) -> Int \{(.*?)\n    \}",
        render, re.S)
    assert m, "Render.rank is gone; display_order mirrors nothing"
    body = " ".join(m.group(1).split())
    assert "spec.pathKeys.contains(key) || spec.patternKeys.contains(key) { return 0 }" in body
    assert "guard spec.blobKeys.contains(key) else { return 10 }" in body
    assert (
        "return destroyedBlobKeys[spec.name]?.contains(key) == true ? 15 : 20"
        in body
    ), body
    assert "ra == rb ? a < b : ra < rb" in " ".join(render.split()), (
        "Render.orderedKeys no longer breaks ties alphabetically")


def test_both_cloud_surfaces_print_the_sheets_order():
    """The two places the model is told about the verbs must agree
    with each other AND with the sheet. They disagreed on exec.shell."""
    from astra.core import system_prompt as sp

    catalogue = physical._catalogue_text()
    prompt = sp.get_system_prompt()
    for v in client.CATALOGUE:
        if not v.arg_keys:
            continue
        order = client.display_order(v)
        bare = ", ".join(order)
        marked_tool = ", ".join(
            (k if k in v.required_keys else f"{k}?")
            + ("*" if k in v.blob_keys else "") for k in order)
        marked_prompt = ", ".join(
            k if k in v.required_keys else f"{k}?" for k in order)
        assert f"{v.name}({marked_tool})" in catalogue, (v.name, bare)
        assert f"`{v.name}({marked_prompt})`" in prompt, (v.name, bare)


@requires_broker
def test_the_payload_summary_the_model_quotes_is_the_one_the_mac_prints():
    """The model quotes Kunal's own approval sheet back to him. That
    quotation is a claim about a program in another repository, and
    nothing in Python runs it, so the only way it can be wrong-and-
    loud instead of wrong-and-silent is to read the renderer.

    IT WAS ALREADY WRONG ONCE. The surface said the sheet reads
    "first <k> of <m> chars" — true of an earlier renderer, false of
    this one, and every test on both sides stayed green while the
    model described to Kunal a prompt he was not looking at.

    Bytes are not a cosmetic choice of unit. One grapheme cluster is
    unbounded in bytes ("A" followed by a cluster carrying 60,000
    combining marks is two characters), so a coverage figure counted
    in characters can read "first 1 of 2" — half — while hiding all
    but one byte of the payload. The number whose entire job is to say
    how much is NOT shown has to be in the unit the file is written
    in.
    """
    render = _code(_RENDER)
    # Render.summaryPrefix, verbatim. Each of these IS a line Kunal
    # reads; if one moves, the template below is describing a sheet
    # that no longer exists.
    for fragment in (
        r'"\(bytes) bytes, sha256 \(digest8), "',
        r'return head + "first \(shownBytes) of \(bytes) bytes: "',
        r'return head + "all \(bytes) bytes: "',
        r'return head + "empty"',
    ):
        assert fragment in render, (
            f"Render.summaryPrefix no longer prints {fragment!r}; the "
            "payload summary quoted to the model is now fiction"
        )

    surface = physical._catalogue_text() + "\n" + physical._SUBMIT_DESCRIPTION
    assert "first <k> of <n> bytes: " in surface, (
        "the model is not told the shape of the payload line at all")
    for wrong in ("of <m> chars", "of <n> chars", "of <k> chars"):
        assert wrong not in surface, (
            f"the surface still quotes {wrong!r}; the Mac counts bytes")


@requires_broker
def test_the_preview_room_refusal_is_described_and_not_invented():
    """A long path leaves too little of the 400-character sheet for
    the payload preview, and the Mac refuses the intent rather than
    showing a preview too short to be consent. That refusal happens
    BEFORE the prompt, so nothing is filed, no fingerprint is asked
    and no irreversible unit is spent — and a model that does not know
    the cause will either retry it or tell Kunal something is waiting
    on him. Both are confabulations with a straight face.
    """
    render = _code(_RENDER)
    assert re.search(r"minContentPreviewChars\s*=\s*\d+", render), (
        "the minimum-preview rule is gone from the renderer; this "
        "description now promises a refusal that cannot happen")
    d = physical._SUBMIT_DESCRIPTION
    assert "too little room to show any of the payload" in d
    assert "nothing was filed, nothing was spent" in d


@requires_broker
def test_the_content_class_matches_both_swift_declarations():
    """The class is declared twice on the Mac on purpose — per verb in
    VerbSpec.blobKeys (the fact the code acts on) and as a flat
    Canonical.contentKeys literal (what Python can parse) — and
    Catalog.assertWellFormed() asserts they agree at every boot. The
    cloud mirrors the per-verb form, so check it against both."""
    catalog = _code(_CATALOG)
    per_verb: dict[str, frozenset[str]] = {}
    for m in re.finditer(
        r'name:\s*"([^"]+)".*?blobKeys:\s*\[([^\]]*)\]', catalog, re.S
    ):
        per_verb[m.group(1)] = frozenset(re.findall(r'"([^"]+)"', m.group(2)))
    assert len(per_verb) == len(client.CATALOGUE), (
        f"parsed blobKeys for {sorted(per_verb)}; the mirror has "
        f"{[v.name for v in client.CATALOGUE]}. A parse that finds fewer "
        "verbs than exist is a broken regex, not a passing test."
    )
    for spec in client.CATALOGUE:
        assert per_verb[spec.name] == spec.blob_keys, spec.name

    flat = re.search(
        r"contentKeys:\s*Set<String>\s*=\s*\[([^\]]*)\]", _code(_CANON))
    assert flat, "Canonical.contentKeys not found"
    assert frozenset(re.findall(r'"([^"]+)"', flat.group(1))) == frozenset(
        k for v in client.CATALOGUE for k in v.blob_keys)


@requires_broker
def test_the_policy_numbers_the_model_is_told_come_from_swift():
    catalog = _code(_CATALOG)
    m = re.search(r"dailyIrreversibleMax:\s*UInt16\s*=\s*(\d+)", catalog)
    assert m, "dailyIrreversibleMax not found"
    assert int(m.group(1)) == client.DAILY_IRREVERSIBLE_MAX

    m = re.search(
        r"requiresSecondConfirmation:\s*Set<String>\s*=\s*\[([^\]]*)\]", catalog)
    assert m, "requiresSecondConfirmation not found"
    assert frozenset(re.findall(r'"([^"]+)"', m.group(1))) == \
        client.SECOND_CONFIRMATION_VERBS

    render = _code(_RENDER)
    m = re.search(r"maxDisplayChars\s*=\s*(\d+)", render)
    assert m and int(m.group(1)) == client.DISPLAY_MAX_CHARS
    m = re.search(r"maxContentPreviewChars\s*=\s*(\d+)", render)
    assert m and int(m.group(1)) == client.CONTENT_PREVIEW_CHARS

    if _SHELL.is_file():
        m = re.search(r"maxTimeoutMs\s*=\s*([\d_]+)", _code(_SHELL))
        assert m, "ShellJail.maxTimeoutMs not found"
        assert int(m.group(1).replace("_", "")) == client.EXEC_SHELL_MAX_TIMEOUT_MS


@requires_broker
def test_the_receipt_outcome_offset_is_the_byte_the_executor_signs():
    """Reading the wrong byte would report a PARTIAL write as a clean
    success — Kunal left with a file in his own repo he cannot write,
    and nothing anywhere saying so. Token.swift's layout comment
    carries the running offset after each field; the outcome ends at
    78, so it is byte 77."""
    src = _TOKEN.read_text()
    # Scope to the Receipt struct: CapabilityToken above it has a
    # `size` of its own, and a regex that found THAT would have passed
    # while pinning the wrong number.
    src = src[src.index("public struct Receipt"):]
    m = re.search(r"b\.append\(outcome\.rawValue\)\s*//\s*\d+\s*->\s*(\d+)", src)
    assert m, "the outcome's offset comment in Token.swift changed shape"
    assert int(m.group(1)) - 1 == client._RECEIPT_OUTCOME_OFFSET
    m = re.search(r"static let size\s*=\s*(\d+)", src)
    assert m and int(m.group(1)) == client._RECEIPT_SIZE
    # The enum's own numbering.
    for name, value in (("ok", 0), ("refused", 1), ("error", 2), ("partial", 3)):
        assert client._RECEIPT_OUTCOMES[value] == name


def test_receipt_outcome_reads_the_signed_byte_and_never_guesses():
    def receipt(outcome: int, size: int = client._RECEIPT_SIZE) -> dict:
        b = bytearray(size)
        if size > client._RECEIPT_OUTCOME_OFFSET:
            b[client._RECEIPT_OUTCOME_OFFSET] = outcome
        return {"receipt_bytes": bytes(b)}

    assert client.receipt_outcome(receipt(0)) == "ok"
    assert client.receipt_outcome(receipt(3)) == "partial"
    assert client.receipt_outcome(receipt(1)) == "refused"
    assert client.receipt_outcome(receipt(2)) == "error"
    # Unreadable is "", never a cheerful default.
    assert client.receipt_outcome(receipt(9)) == ""
    assert client.receipt_outcome(receipt(0, size=100)) == ""
    assert client.receipt_outcome({}) == ""
    assert client.receipt_outcome({"receipt_bytes": None}) == ""


# ── the payload rule ──────────────────────────────────────


def _validate(verb: str, args: dict) -> None:
    client.validate_args(client.CATALOGUE_BY_NAME[verb], args)


def test_a_payload_must_be_a_string():
    for bad in (5, True, 1.5, None, ["x"], {"a": 1}):
        with pytest.raises(Exception):
            _validate("fs.write", {"path": "/private/tmp/f", "content": bad})
    _validate("fs.write", {"path": "/private/tmp/f", "content": "ok"})
    # An empty payload truncates a file. It is a write, not a mistake.
    _validate("fs.write", {"path": "/private/tmp/f", "content": ""})


def test_a_decomposed_payload_is_refused_rather_than_normalised():
    """ACE-1 hashes strings as NFC, so a decomposed payload would be
    SIGNED as its composed form: the bytes on disk would not be the
    bytes sent, and a later fs.edit against the model's own copy would
    find zero occurrences with no explanation."""
    nfd = "café"
    nfc = "café"
    assert unicodedata.normalize("NFC", nfd) == nfc and nfd != nfc

    with pytest.raises(client.ArgsInvalid) as e:
        _validate("fs.write", {"path": "/private/tmp/f", "content": nfd})
    assert "NFC" in str(e.value)
    assert "content" in str(e.value)
    _validate("fs.write", {"path": "/private/tmp/f", "content": nfc})

    with pytest.raises(client.ArgsInvalid):
        _validate("fs.edit", {"path": "/private/tmp/f", "old": nfd, "new": "x"})
    with pytest.raises(client.ArgsInvalid):
        _validate("fs.edit", {"path": "/private/tmp/f", "old": "x", "new": nfd})
    _validate("fs.edit", {"path": "/private/tmp/f", "old": nfc, "new": "x"})

    # A PATH is not a payload: decomposed and composed name one file,
    # and the Mac normalises it silently. The rule must not leak.
    _validate("fs.read", {"path": "/private/tmp/café.txt"})


def test_the_secret_name_gate_governs_writes_exactly_as_it_governs_reads():
    for path in ("/private/tmp/p/.env.local", "/private/tmp/p/credentials.json",
                 "/private/tmp/p/.git/config", "/private/tmp/p/id_rsa"):
        with pytest.raises(client.ArgsInvalid):
            _validate("fs.write", {"path": path, "content": "x"})
        with pytest.raises(client.ArgsInvalid):
            _validate("fs.edit", {"path": path, "old": "a", "new": "b"})


def test_a_write_outside_the_compiled_roots_is_refused_before_filing():
    with pytest.raises(client.ArgsInvalid):
        _validate("fs.write", {"path": "/etc/hosts", "content": "x"})
    with pytest.raises(client.ArgsInvalid):
        _validate("exec.shell", {"command": "ls", "cwd": "/etc"})


def test_exec_shell_requires_a_cwd_and_an_integer_timeout():
    with pytest.raises(client.ArgsInvalid) as e:
        _validate("exec.shell", {"command": "git status"})
    assert "cwd" in str(e.value)
    _validate("exec.shell", {"command": "git status", "cwd": "/private/tmp"})
    for bad in ("5000", -1, 1.5, True):
        with pytest.raises(client.ArgsInvalid):
            _validate("exec.shell", {"command": "ls", "cwd": "/private/tmp",
                                     "timeout_ms": bad})
    # 0 and 550000 are CANONICAL — integers, which is the whole point
    # of the shared boundary corpus (JSON 0 read as a boolean was a
    # real outage) — and both are outside the range exec.shell
    # declares. The two layers are asserted separately, in the order
    # the Mac runs them, because a single function that refused them
    # both would make the corpus unable to say that 0 canonicalises.
    for out_of_range in (0, client.EXEC_SHELL_MAX_TIMEOUT_MS * 10):
        args = {"command": "ls", "cwd": "/private/tmp",
                "timeout_ms": out_of_range}
        _validate("exec.shell", args)
        with pytest.raises(client.PreconditionFailed) as e:
            client.precheck_args(client.CATALOGUE_BY_NAME["exec.shell"], args)
        assert f"1…{client.EXEC_SHELL_MAX_TIMEOUT_MS}" in str(e.value)
        # The number is REFUSED, never adjusted: the sheet must not
        # state a timeout the body will not honour.
        assert "adjusting" in str(e.value)
    _validate("exec.shell", {"command": "ls", "cwd": "/private/tmp",
                             "timeout_ms": 1})
    client.precheck_args(client.CATALOGUE_BY_NAME["exec.shell"],
                         {"command": "ls", "cwd": "/private/tmp",
                          "timeout_ms": client.EXEC_SHELL_MAX_TIMEOUT_MS})
