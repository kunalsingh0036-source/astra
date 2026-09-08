"""Every integer argument's declared RANGE, pinned to the Swift literal.

An integer argument with a type and no bound is half a contract, and
this repo has paid for that half twice on the same shape:

  `fs.read.limit` was untyped. "800" canonicalised, got signed, and was
  then ignored by the executor.

  `exec.shell.timeout_ms` was typed and unbounded. `timeout_ms: 300000`
  rendered VERBATIM on both Touch ID sheets, cost two fingerprints and
  one of the day's three irreversible units, and was then cut to 55 s
  inside the jail — the command SIGKILLed mid-way, the clamp announced
  afterwards in the result text. A sheet that states a number the body
  will not honour is a sheet that lies, and it lied about the one verb
  whose whole approval story is "the command you read is the command
  that runs".

Both are closed by `VerbSpec.intBounds` in Catalog.swift, which
`Catalog.assertWellFormed` requires to be complete over `intKeys` — so
on the Mac an integer argument cannot exist without a range. That
completeness is worth nothing to the cloud if the MIRROR is a
hand-typed copy that someone remembers to update: the failure mode is
silent, and it is the class this repo already names (a guard gated on
a hand-maintained list is not a guard). So the numbers are read out of
the Swift source and compared, and a bound whose expression this file
cannot resolve FAILS rather than being skipped.

WHY THE CHECK IS NOT IN `validate_args`. That function mirrors the
CANONICALISER, and it is pinned against it by one shared corpus of
boundary values (Resources/arg_boundary_cases.json) — the file that
exists because the two implementations of that one rule disagreed
about `0` for months. `timeout_ms: 0` IS canonical: reading it as a
boolean was the bug. It is refused one layer later, by
`Precondition.check`, before the sheet is composed and before the
budget is spent. `client.precheck_args` mirrors that layer, so both
statements can be true at once and neither corpus has to lie.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from astra.broker import client

_ASTRA_ROOT = pathlib.Path(__file__).resolve().parents[2]
_BROKER = _ASTRA_ROOT.parent / "astra-broker"
_CATALOG = _BROKER / "Sources/AstraCore/Catalog.swift"
_SHELL = _BROKER / "Sources/AstraCore/ShellVerb.swift"
_FILEVERBS = _BROKER / "Sources/AstraCore/FileVerbs.swift"
_PRECONDITION = _BROKER / "Sources/AstraCore/Precondition.swift"

requires_broker = pytest.mark.skipif(
    not all(p.is_file() for p in (_CATALOG, _SHELL, _FILEVERBS,
                                  _PRECONDITION)),
    reason=f"astra-broker is not checked out beside astra at {_BROKER}; "
           "the integer-bounds mirror is unpinned in this checkout",
)


def _code(p: pathlib.Path) -> str:
    """Swift with line comments stripped.

    Guards read CODE. A guard that matched the comment explaining a
    rule would pass on a file where the rule had been deleted and the
    paragraph left behind, which is the shape of every stale claim
    this codebase has had to retract.
    """
    return re.sub(r"//[^\n]*", "", p.read_text())


# The Swift constants an intBounds expression is allowed to name, and
# where each is defined. Resolved from the source rather than typed
# here for the same reason the bounds are: a second copy of a number is
# a second thing to forget.
_SYMBOLS = {
    "ShellJail.maxTimeoutMs": (_SHELL, r"maxTimeoutMs\s*=\s*([\d_]+)"),
    "FileVerbs.maxReadLimit": (_FILEVERBS,
                               r"maxReadLimit\s*=\s*(\d+)\s*\*\s*(\d+)"),
}


def _symbol(name: str) -> int:
    path, pattern = _SYMBOLS[name]
    m = re.search(pattern, _code(path))
    assert m, f"{name} not found in {path.name}"
    value = 1
    for g in m.groups():
        value *= int(g.replace("_", ""))
    return value


def _swift_int(expr: str) -> int:
    """One end of a Swift range literal, as a number.

    Deliberately narrow: a decimal literal, a `1 << n` shift, or
    `Int64(X.y)` naming a constant in `_SYMBOLS`. Anything else raises,
    because the alternative to failing on an unknown expression is
    returning a number that is not the one in the binary.
    """
    e = expr.strip()
    # Unwrap only a paren that encloses the WHOLE expression
    # (`(1 << 40)`), never a trailing one that belongs to a call —
    # stripping the character set off both ends turned
    # `Int64(FileVerbs.maxReadLimit)` into an unparsable stub.
    while e.startswith("(") and e.endswith(")") and "(" not in e[1:-1]:
        e = e[1:-1].strip()
    m = re.fullmatch(r"Int64\(\s*([\w.]+)\s*\)", e)
    if m:
        return _symbol(m.group(1))
    m = re.fullmatch(r"(\d[\d_]*)\s*<<\s*(\d+)", e)
    if m:
        return int(m.group(1).replace("_", "")) << int(m.group(2))
    m = re.fullmatch(r"(\d[\d_]*)", e)
    if m:
        return int(m.group(1).replace("_", ""))
    raise AssertionError(
        f"cannot resolve the Swift bound expression {expr!r}. Teach this "
        "parser the form — do NOT skip it: an unresolved bound is a bound "
        "the cloud mirror is not being checked against."
    )


def _swift_bounds() -> dict[str, dict[str, tuple[int, int]]]:
    """verb -> {argument: (lo, hi)} straight out of Catalog.swift."""
    src = _code(_CATALOG.read_text() and _CATALOG)
    out: dict[str, dict[str, tuple[int, int]]] = {}
    for m in re.finditer(
        r'name:\s*"([\w.]+)".*?intBounds:\s*(\[[^\]]*\])', src, re.S,
    ):
        verb, literal = m.group(1), m.group(2)
        entries: dict[str, tuple[int, int]] = {}
        for e in re.finditer(r'"([^"]+)"\s*:\s*([^,\]]+)', literal):
            lo, _, hi = e.group(2).partition("...")
            assert hi, (
                f'{verb}.{e.group(1)} bound {e.group(2)!r} is not a closed '
                "range; only `lo...hi` is mirrored"
            )
            entries[e.group(1)] = (_swift_int(lo), _swift_int(hi))
        out[verb] = entries
    return out


@requires_broker
def test_the_parser_matched_something_believable():
    """A parse of nothing must never read as "nothing is bounded"."""
    swift = _swift_bounds()
    assert len(swift) == len(client.CATALOGUE), (
        f"parsed {len(swift)} verbs out of Catalog.swift, the mirror has "
        f"{len(client.CATALOGUE)}. The literal grew a field this regex does "
        "not know about — fix the regex, do not relax it."
    )
    bounded = {v: b for v, b in swift.items() if b}
    assert bounded, "no verb parsed with any bound; the regex has gone blind"
    assert "exec.shell" in bounded, "exec.shell's timeout_ms bound vanished"


@requires_broker
def test_every_swift_bound_is_mirrored_with_the_same_numbers():
    swift = _swift_bounds()
    for spec in client.CATALOGUE:
        mine = {k: (lo, hi) for k, lo, hi in spec.int_bounds}
        assert mine == swift[spec.name], (
            f"{spec.name}: the cloud mirror declares {mine}, Catalog.swift "
            f"declares {swift[spec.name]}. The cloud would file an intent "
            "the Mac refuses at its precheck, or refuse one it would have "
            "run."
        )


@requires_broker
def test_every_integer_argument_has_a_bound_on_both_sides():
    """Completeness, mirroring Catalog.assertWellFormed.

    The Mac refuses to BOOT with an intKey that has no range. The cloud
    cannot refuse to boot, so it fails here instead — otherwise a new
    integer argument is typed on both sides and bounded on one, which
    is exactly the shape `timeout_ms` had.
    """
    for spec in client.CATALOGUE:
        assert {k for k, _, _ in spec.int_bounds} == spec.int_keys, (
            f"{spec.name}: int_keys {sorted(spec.int_keys)} but bounds for "
            f"{sorted(k for k, _, _ in spec.int_bounds)}"
        )


@requires_broker
def test_the_shell_timeout_ceiling_is_the_jails_own_ceiling():
    """One number, not two that have to agree.

    `ShellJail.maxTimeoutMs` is what the jail enforces; exec.shell's
    declared range is what Kunal is allowed to be shown. If the mirror
    ever states a larger ceiling than the jail's, the sheet can again
    promise a duration the body will not honour.
    """
    assert client.EXEC_SHELL_MAX_TIMEOUT_MS == _symbol("ShellJail.maxTimeoutMs")
    hi = dict((k, h) for k, _, h in
              client.CATALOGUE_BY_NAME["exec.shell"].int_bounds)
    assert hi["timeout_ms"] == client.EXEC_SHELL_MAX_TIMEOUT_MS


@requires_broker
def test_the_mac_refuses_out_of_range_rather_than_clamping():
    """The behaviour, not just the number.

    A ceiling the executor CLAMPS to and a ceiling it REFUSES at are
    different products: one spends two fingerprints and a budget unit
    before disappointing Kunal, the other costs nothing. The cloud's
    wording promises the second, so the Swift side is read to check it
    still is one — `min(`/`max(` on the requested timeout is the shape
    of the version that lied.
    """
    shell = _code(_SHELL)
    m = re.search(r"let\s+effective\s*=\s*([^\n]+)", shell)
    assert m, "ShellVerb no longer computes an `effective` timeout"
    assert "min(" not in m.group(1) and "max(" not in m.group(1), (
        f"the jail is clamping again: `{m.group(1).strip()}`. The number on "
        "the Touch ID sheet is the number that must run, so an out-of-range "
        "timeout is refused at Precondition, never adjusted here."
    )
    assert "intBounds" in _code(_PRECONDITION), (
        "Precondition.check no longer consults VerbSpec.intBounds, so an "
        "out-of-range integer would reach the sheet"
    )


# ── the cloud half: same verdict, one round trip earlier ──


def test_precheck_refuses_both_ends_and_names_the_range():
    spec = client.CATALOGUE_BY_NAME["exec.shell"]
    for bad in (0, -5, client.EXEC_SHELL_MAX_TIMEOUT_MS + 1):
        with pytest.raises(client.PreconditionFailed) as e:
            client.precheck_args(spec, {"command": "ls", "cwd": "/private/tmp",
                                        "timeout_ms": bad})
        assert f"1…{client.EXEC_SHELL_MAX_TIMEOUT_MS}" in str(e.value)
        assert "Nothing was filed" in str(e.value)


def test_precheck_accepts_the_endpoints():
    spec = client.CATALOGUE_BY_NAME["exec.shell"]
    for ok in (1, client.EXEC_SHELL_MAX_TIMEOUT_MS):
        client.precheck_args(spec, {"command": "ls", "cwd": "/private/tmp",
                                    "timeout_ms": ok})


def test_precheck_covers_the_read_ceiling_that_left_the_canonicaliser_table():
    """`fs.read.limit` over the executor's ceiling, at its own layer.

    This case lived in the canonicaliser table, where it never
    belonged: the Mac types the integer at canonicalisation and bounds
    it afterwards. Same property, correct layer.
    """
    spec = client.CATALOGUE_BY_NAME["fs.read"]
    client.validate_args(spec, {"path": "/private/tmp/a", "limit": 700000})
    with pytest.raises(client.PreconditionFailed) as e:
        client.precheck_args(spec, {"path": "/private/tmp/a", "limit": 700000})
    assert str(client.FS_READ_MAX_LIMIT) in str(e.value)
    client.precheck_args(spec, {"path": "/private/tmp/a",
                                "limit": client.FS_READ_MAX_LIMIT})


def test_precheck_ignores_a_boolean_rather_than_reading_it_as_zero():
    """`True` is not 0 or 1 here.

    bool subclasses int in Python, so a naive range check reads JSON
    `true` as 1 and lets it through — the mirror image of the Swift
    bug where NSNumber(0) bridged to Bool and `offset: 0` was refused.
    A boolean is a TYPE error, refused by validate_args with the key
    named; this layer must not pre-empt that with a range verdict.
    """
    spec = client.CATALOGUE_BY_NAME["exec.shell"]
    client.precheck_args(spec, {"command": "ls", "cwd": "/private/tmp",
                                "timeout_ms": True})
    with pytest.raises(client.ArgsInvalid) as e:
        client.validate_args(spec, {"command": "ls", "cwd": "/private/tmp",
                                    "timeout_ms": True})
    assert "timeout_ms" in str(e.value)


@requires_broker
def test_the_sheet_shape_caps_match_render_swift():
    """`maxDisplayLines` and `maxDisplayLineChars`, mirrored.

    These bound the sheet's SHAPE rather than its size, and they are
    the two that can refuse an otherwise-valid intent: a path or an
    `exec.shell` command wider than one row is refused even when the
    whole sheet would fit inside 400 characters. A model that does not
    know the width cap files a long command, is refused
    `unrenderable`, and has no idea which of its arguments was the
    problem — so the number has to be in the description, and a number
    in a description is a copy that must be checked.
    """
    render = _code(_BROKER / "Sources/AstraCore/Render.swift")
    for name, mine in (("maxDisplayLines", client.DISPLAY_MAX_LINES),
                       ("maxDisplayLineChars", client.DISPLAY_MAX_LINE_CHARS)):
        m = re.search(rf"let {name}\s*=\s*(\d+)", render)
        assert m, f"Render.{name} not found"
        assert int(m.group(1)) == mine, (
            f"Render.{name} is {m.group(1)}, the cloud mirror says {mine}")


def test_the_width_cap_is_stated_where_the_model_reads_it():
    from astra.core import system_prompt
    from astra.runtime.tools import physical

    n = str(client.DISPLAY_MAX_LINE_CHARS)
    assert n in physical._SUBMIT_DESCRIPTION
    assert n in system_prompt.get_system_prompt()
    # The budget for a VALUE is the line cap minus its label, and the
    # description quotes it for `command` — the field long enough to
    # hit it. If the label arithmetic drifts the quoted number is
    # wrong by a few characters and the model splits work it need not.
    assert client.display_line_budget("command") == (
        client.DISPLAY_MAX_LINE_CHARS - len("  command: ") - 1)
    assert str(client.display_line_budget("command")) in \
        physical._SUBMIT_DESCRIPTION


def test_the_model_is_never_told_the_timeout_is_clamped():
    """The two surfaces that teach the model what a number does.

    A clamp and a refusal are different products. While the tool
    description and the system prompt said "clamped", the model had
    every reason to send 300000 — and that intent cost two
    fingerprints and one of the day's three irreversible units before
    the jail cut the command off mid-way. The wording is asserted, not
    just the number, because the number was right the whole time.
    """
    from astra.core import system_prompt
    from astra.runtime.tools import physical

    lo, hi = 1, client.EXEC_SHELL_MAX_TIMEOUT_MS
    for name, text in (
        ("the submit_intent description", physical._SUBMIT_DESCRIPTION),
        ("the system prompt", system_prompt.get_system_prompt()),
    ):
        window = text[max(0, text.find("timeout_ms") - 400):]
        assert "clamp" not in window.lower(), (
            f"{name} still describes timeout_ms as clamped; the Mac refuses "
            "an out-of-range value at its precheck and never adjusts it"
        )
        assert f"{lo} and {hi}" in text, (
            f"{name} does not state the {lo}…{hi} range the mirror declares"
        )


def test_precheck_is_silent_on_verbs_with_no_integer_arguments():
    for name in ("fs.write", "fs.edit", "fs.glob", "body.probe"):
        client.precheck_args(client.CATALOGUE_BY_NAME[name], {"anything": 1})
