"""The cloud's catalogue mirror is pinned to the Swift sources.

astra/broker/client.py carries a copy of five things compiled into the
Mac binaries: the verb table (Catalog.swift: policy, argument keys,
required keys, path/pattern/int classification, size caps), which
verbs execute() handles (AstraExecutor/main.swift), the compiled roots
and name-denial tables (Canonicalizer.swift), the fs.read ceiling
(FileVerbs.swift) and the auto-lane rate ceiling (RateLimit.swift).
Until the broker publishes a per-verb `wired` flag this mirror is the
only place the cloud knows any of it, and nothing else would go red if
the repos drifted: the Swift side wires fs.grep, the cloud keeps
refusing it as unwired forever; the Swift side renames an argument,
the cloud refuses every valid intent as "not an argument of".

This test parses the Swift files (comments stripped, literal tables
read) and compares. It SKIPS with a named reason when the sibling
checkout is absent (CI for astra alone) and never passes silently.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from astra.broker import client

_ASTRA_ROOT = pathlib.Path(__file__).resolve().parents[2]
_BROKER = _ASTRA_ROOT.parent / "astra-broker"
_CATALOG = _BROKER / "Sources/AstraCore/Catalog.swift"
_EXECUTOR = _BROKER / "Sources/AstraExecutor/main.swift"
_CANON = _BROKER / "Sources/AstraCore/Canonicalizer.swift"
_FILEVERBS = _BROKER / "Sources/AstraCore/FileVerbs.swift"
_RATELIMIT = _BROKER / "Sources/AstraCore/RateLimit.swift"
_RENDER = _BROKER / "Sources/AstraCore/Render.swift"
_SHELL = _BROKER / "Sources/AstraCore/ShellVerb.swift"

requires_broker = pytest.mark.skipif(
    not all(p.is_file() for p in (_CATALOG, _EXECUTOR, _CANON, _FILEVERBS,
                                  _RATELIMIT, _RENDER, _SHELL)),
    reason=f"astra-broker not checked out beside astra at {_BROKER}; the "
           "catalogue mirror is unpinned in this checkout",
)


def _strip_comments(src: str) -> str:
    return re.sub(r"//[^\n]*", "", src)


def _strings(swift_list: str) -> frozenset[str]:
    return frozenset(re.findall(r'"([^"]+)"', swift_list))


def _bounds(swift_dict: str) -> dict[str, str]:
    """`["timeout_ms": 1...Int64(ShellJail.maxTimeoutMs)]` -> the raw range.

    Keys and RAW expressions, not numbers: the bounds name constants
    defined in other Swift files and resolving them belongs in one
    place (test_int_bounds_mirror.py, which pins the numbers). Here the
    keys alone are already worth comparing — Catalog.assertWellFormed
    requires intBounds to be complete over intKeys, so a missing key on
    this side is an integer argument the Mac bounds and the cloud does
    not, i.e. one the cloud files and the Mac refuses at its precheck.
    """
    if swift_dict.strip() in ("[:]", "[]"):
        return {}
    return {
        m.group(1): m.group(2).strip()
        for m in re.finditer(r'"([^"]+)"\s*:\s*([^,\]]+)', swift_dict)
    }


def _parse_catalog(src: str) -> list[dict]:
    src = _strip_comments(src)
    pat = re.compile(
        r'VerbSpec\(\s*id:\s*(\d+),\s*name:\s*"([^"]+)",\s*policy:\s*\.(\w+),'
        r'\s*degradation:\s*\.(\w+),\s*maxArgBytes:\s*([\d_]+),'
        r'\s*irreversible:\s*(true|false),\s*maxRuntimeMs:\s*([\d_]+),'
        r'\s*runsInJail:\s*(true|false),'
        r'\s*argKeys:\s*\[([^\]]*)\],\s*requiredKeys:\s*\[([^\]]*)\],'
        r'\s*pathKeys:\s*\[([^\]]*)\],\s*patternKeys:\s*\[([^\]]*)\],'
        r'\s*intKeys:\s*\[([^\]]*)\],\s*intBounds:\s*(\[[^\]]*\]),'
        r'\s*blobKeys:\s*\[([^\]]*)\],'
        r'\s*enumKeys:\s*\[([^\]]*)\]\s*\)', re.S,
    )
    out = []
    for m in pat.finditer(src):
        out.append({
            "id": int(m.group(1)), "name": m.group(2), "policy": m.group(3),
            "degradation": m.group(4),
            "max_arg_bytes": int(m.group(5).replace("_", "")),
            "arg_keys": _strings(m.group(9)),
            "required_keys": _strings(m.group(10)),
            "path_keys": _strings(m.group(11)),
            "pattern_keys": _strings(m.group(12)),
            "int_keys": _strings(m.group(13)),
            # The DECLARED RANGE of each integer argument, as
            # `key -> the Swift expression for lo...hi`. Kept raw here
            # because the expressions name constants in other files
            # (`Int64(ShellJail.maxTimeoutMs)`); test_int_bounds_mirror.py
            # resolves them and pins the numbers. Catalog.swift asserts
            # at boot that intBounds is complete over intKeys, so the
            # keys alone are worth comparing: an integer argument that
            # gained a bound on the Mac and not here is one the cloud
            # files and the Mac refuses at its precheck.
            "int_bounds": _bounds(m.group(14)),
            # The CONTENT CLASS. Mirrored because two rules hang off it
            # and both are visible from the cloud: the approval sheet
            # summarises a payload instead of showing it whole (which
            # is what makes a 1 MiB fs.write renderable at all), and a
            # payload must already be NFC or it is refused. A verb that
            # gained a blob key here and not in the mirror would have
            # the cloud refusing valid writes, or accepting a payload
            # the Mac will refuse.
            "blob_keys": _strings(m.group(15)),
            # A closed-enum argument: the executor resolves the name to
            # a compiled path. Mirrored so the cloud cannot start
            # treating one as a free string.
            "enum_keys": _strings(m.group(16)),
            "irreversible": m.group(6) == "true",
            # The verb's COMPILED runtime bound. It is not a number the
            # cloud sends or can influence — the point of it being here
            # is that it used to live in the token, where a broker byte
            # decided how long the body would wait before reporting an
            # action abandoned while the jailed child ran on. Parsed so
            # test_write_verbs can pin the one relation the cloud
            # depends on: no `timeout_ms` this module will file can
            # outlast it.
            "max_runtime_ms": int(m.group(7).replace("_", "")),
        })
    return out


def _parse_executor_cases(src: str) -> frozenset[str]:
    """The executor's wired set, as the source declares it.

    Three forms have existed, and the parser reads all of them because
    a parser that silently matched none returned the empty set and
    would have read as "the executor wires nothing":

      `let alwaysWired: [String: VerbImpl] = [...]`  (current) plus the
          entries `wiredTable()` adds at boot behind a gate. exec.shell
          is one of those: it is registered only if the sandbox-exec
          canary passes, so it is a RUNTIME fact the source can only
          declare conditionally. The set the executor actually reports
          in each challenge reply is the authority at run time; this is
          what the build can do.
      `let wired: [String: VerbImpl] = [...]`        (previous)
      `case "verb":` labels inside execute()         (original)
    """
    src = _strip_comments(src)
    names: set[str] = set()
    for name in ("alwaysWired", "wired"):
        m = re.search(
            rf"let {name}:\s*\[String:\s*VerbImpl\]\s*=\s*\[(.*?)\n\]",
            src, re.S,
        )
        if m:
            names |= set(re.findall(r'"([\w.]+)"\s*:', m.group(1)))
            break
    # Conditionally-registered verbs: `t["exec.shell"] = execShell`.
    names |= set(re.findall(r'\w+\["([\w.]+)"\]\s*=\s*\w+', src))
    if names:
        return frozenset(names)
    start = src.index("func execute(")
    end = src.index("func executeBounded(")
    body = src[start:end]
    return frozenset(re.findall(r'case\s+"([\w.]+)"\s*:', body))


def test_executor_parser_reads_every_form():
    table = 'let wired: [String: VerbImpl] = [\n    "fs.read": fsRead,\n    "fs.glob": fsGlob,\n]\n'
    assert _parse_executor_cases(table) == {"fs.read", "fs.glob"}
    gated = (
        'let alwaysWired: [String: VerbImpl] = [\n    "fs.read": fsRead,\n]\n'
        'func wiredTable() -> X {\n    if c.ok { t["exec.shell"] = execShell }\n}\n'
    )
    assert _parse_executor_cases(gated) == {"fs.read", "exec.shell"}
    switch = (
        'func execute(spec: VerbSpec) -> X {\n    switch spec.name {\n'
        '    case "a.b":\n        return 1\n    default:\n        return 0\n    }\n}\n'
        'func executeBounded() {}\n'
    )
    assert _parse_executor_cases(switch) == {"a.b"}


def _swift_table(src: str, name: str) -> frozenset[str]:
    src = _strip_comments(src)
    m = re.search(
        rf"{name}\s*:\s*(?:Set<String>|\[String\])\s*=\s*\[(.*?)\]", src, re.S,
    )
    assert m, f"{name} not found in Swift source"
    return _strings(m.group(1))


@requires_broker
def test_verb_table_matches_catalog_swift():
    swift = _parse_catalog(_CATALOG.read_text())
    assert len(swift) == len(client.CATALOGUE), (
        f"Catalog.swift parse found {len(swift)} verbs, the cloud mirror has "
        f"{len(client.CATALOGUE)}. A parse of 0 means the Swift literal grew a "
        f"field this regex does not know about — fix the regex, do not relax it."
    )
    assert len(swift) >= 8, "the parser matched too few verbs to be believable"
    assert [v["name"] for v in swift] == [v.name for v in client.CATALOGUE], (
        "verb order (ids) differs"
    )
    for i, (s, c) in enumerate(zip(swift, client.CATALOGUE)):
        assert s["id"] == i
        assert s["policy"] == c.policy, c.name
        assert s["max_arg_bytes"] == c.max_arg_bytes, c.name
        assert s["arg_keys"] == c.arg_keys, c.name
        assert s["required_keys"] == c.required_keys, c.name
        assert s["path_keys"] == c.path_keys, c.name
        assert s["pattern_keys"] == c.pattern_keys, c.name
        assert s["int_keys"] == c.int_keys, c.name
        assert set(s["int_bounds"]) == {k for k, _, _ in c.int_bounds}, c.name
        assert s["blob_keys"] == c.blob_keys, c.name
        assert s["irreversible"] == c.irreversible, c.name


@requires_broker
def test_wired_set_matches_the_executor_switch():
    cases = _parse_executor_cases(_EXECUTOR.read_text())
    assert cases == client.WIRED_VERBS, (
        f"executor handles {sorted(cases)}, cloud believes "
        f"{sorted(client.WIRED_VERBS)}: run_intent would refuse a wired "
        "verb as unwired (or file one the executor will refuse)"
    )
    assert cases <= set(client.CATALOGUE_BY_NAME)


@requires_broker
def test_name_denial_tables_and_roots_match_canonicalizer_swift():
    src = _CANON.read_text()
    assert _swift_table(src, "forbiddenKeys") == client.FORBIDDEN_ARG_KEYS
    assert _swift_table(src, "allowedRoots") == frozenset(client.ALLOWED_ROOTS)
    assert _swift_table(src, "deniedExact") == client.DENIED_EXACT
    assert _swift_table(src, "deniedPrefixes") == frozenset(client.DENIED_PREFIXES)
    assert _swift_table(src, "deniedBases") == client.DENIED_BASES
    assert _swift_table(src, "deniedBaseWords") == client.DENIED_BASE_WORDS
    assert _swift_table(src, "deniedExtensions") == client.DENIED_EXTENSIONS
    assert _swift_table(src, "sourceExtensions") == client.SOURCE_EXTENSIONS


@requires_broker
def test_read_ceiling_and_auto_rate_match_swift():
    fv = _strip_comments(_FILEVERBS.read_text())
    m = re.search(r"maxReadLimit\s*=\s*(\d+)\s*\*\s*(\d+)", fv)
    assert m, "maxReadLimit not found"
    assert int(m.group(1)) * int(m.group(2)) == client.FS_READ_MAX_LIMIT
    rl = _strip_comments(_RATELIMIT.read_text())
    m = re.search(r"autoPerHour:\s*Int\s*=\s*(\d+)", rl)
    assert m, "autoPerHour not found"
    assert int(m.group(1)) == client.AUTO_INTENTS_PER_HOUR
    read = client.CATALOGUE_BY_NAME["fs.read"]
    assert dict((k, hi) for k, _, hi in read.int_bounds) == {
        "limit": client.FS_READ_MAX_LIMIT,
        "offset": client.FS_READ_MAX_OFFSET,
    }


@requires_broker
def test_the_sheets_arithmetic_matches_swift():
    """Every number the model is told about the approval sheet.

    These are not decoration: the tool description quotes them, and a
    model that believes the wrong cap either splits work that would
    have fitted or files an intent the Mac refuses as `unrenderable`
    after a round trip. `maxWhyLineChars` is the newest and the one
    most likely to drift, because it is a RESERVATION rather than a
    limit on anything the model can see.
    """
    r = _strip_comments(_RENDER.read_text())
    want = {
        "maxDisplayChars": client.DISPLAY_MAX_CHARS,
        "maxContentPreviewChars": client.CONTENT_PREVIEW_CHARS,
        # the reservation, minus the label and the newline the Mac adds
        "maxWhyLineChars": client.WHY_MAX_CHARS + len("  because: ") + 1,
    }
    for name, expected in want.items():
        m = re.search(rf"let {name}\s*=\s*(\d+)", r)
        assert m, f"{name} not found in Render.swift"
        assert int(m.group(1)) == expected, (
            f"Render.{name} is {m.group(1)}, the cloud mirror says {expected}")
    sh = _strip_comments(_SHELL.read_text())
    m = re.search(r"maxTimeoutMs\s*=\s*([\d_]+)", sh)
    assert m, "ShellJail.maxTimeoutMs not found"
    assert int(m.group(1).replace("_", "")) == client.EXEC_SHELL_MAX_TIMEOUT_MS


@requires_broker
@pytest.mark.parametrize("component,denied", [
    (".env.local", True), ("credentials.json", True), ("gmail_token.json", True),
    ("firebase-adminsdk-x.json", True), ("server.pem.bak", True),
    ("id_rsa", True), ("CREDENTIALS.md", True), (".cloudflare-api-token", True),
    ("Token.swift", False), ("tokens.css", False), ("secrets.ts", False),
    ("tokens", False), ("chat.txt", False), ("README.md", False),
    # Canonicalizer.swift's comment says `credentials.py` is refused;
    # its CODE (`deniedBases.contains(base) && !isSource`) does not
    # refuse a source file by base. The mirror follows the code
    # (guards read code, not comments); the table-equality test above
    # keeps both sides identical, and the comment is flagged for the
    # Swift side to reconcile.
    ("credentials.py", False),
])
def test_denial_reason_mirrors_the_swift_examples(component, denied):
    """The worked examples in Canonicalizer.swift's own comment block."""
    assert (client.denial_reason(component) is not None) is denied, component


def test_catalogue_derived_sets_are_consistent():
    """Boot-style assertions the Swift side makes in assertWellFormed."""
    for v in client.CATALOGUE:
        assert v.required_keys <= v.arg_keys, v.name
        assert v.path_keys <= v.arg_keys and v.pattern_keys <= v.arg_keys, v.name
        assert v.path_keys.isdisjoint(v.pattern_keys), v.name
        assert v.int_keys <= v.arg_keys, v.name
        assert v.int_keys.isdisjoint(v.path_keys | v.pattern_keys), v.name
        assert v.policy in ("auto", "signedNoStanding"), v.name
    assert client.AUTO_VERBS | client.SIGNED_VERBS == set(client.CATALOGUE_BY_NAME)
    assert client.AUTO_VERBS.isdisjoint(client.SIGNED_VERBS)
    assert client.WIRED_VERBS <= set(client.CATALOGUE_BY_NAME)


# ── what renders from the mirror (system prompt, agent_repos) ──
# The prompt served on every turn and the agent_repos fix flow are
# generated from client.CATALOGUE; these pin their per-verb tables to
# the catalogue and their quoted roots and limits to the Swift sources,
# so the model is never taught a verb, root or limit the Mac lacks.


@requires_broker
def test_an_unwired_verb_is_still_refused_honestly_and_earlier():
    """CHARTER §8: nothing silently no-ops.

    This assertion used to read the executor's dispatch fallback, which
    refused an unwired verb with "not wired yet" — AFTER the verifier
    had spent a challenge, a budget unit and the epoch ratchet on it
    (intent #10). A7 moved the refusal to the VERIFIER, before any of
    that, and turned the dispatch fallback into an internal ERROR: a
    policy refusal reached at dispatch is now a bug, not an outcome.

    So the property is pinned where it now lives — and the fallback is
    still checked for being loud, because a silent one is the thing
    §8 forbids."""
    verify = _strip_comments((_BROKER / "Sources/AstraCore/Verify.swift").read_text())
    assert "verb_unavailable" in verify, (
        "the verifier lost its honest refusal for a catalogued verb this "
        "build cannot perform"
    )
    assert "not wired" in verify, "the refusal no longer says what is wrong"

    src = _strip_comments(_EXECUTOR.read_text())
    dispatch = src[src.index("func execute("):src.index("func executeBounded(")]
    assert ".error" in dispatch and "internal" in dispatch, (
        "the executor's dispatch fallback must stay LOUD: a verb that "
        "reaches it has been admitted by a verifier that should have "
        "refused it, which is a bug, and it must never return silently"
    )


@requires_broker
def test_a_wired_verb_that_cannot_run_is_refused_before_the_budget_too():
    """The same rule as verb_unavailable, one rung down.

    A verb can be wired and still impossible: `old` occurring twice,
    a missing parent directory, a cwd that is a file. Those used to be
    discovered inside the ACT — after the fingerprint and after the
    budget spend — which is the intent #10 shape wearing different
    clothes. The refusal is now `precondition_failed`, raised in the
    verifier before step (k) and by the broker before the prompt.

    Pinned from the cloud because the cloud is what TELLS Kunal and
    the model what a refusal cost; if the body stopped checking early
    and this file kept saying it did, every wrong `old` would silently
    start costing a unit again."""
    verify = _strip_comments((_BROKER / "Sources/AstraCore/Verify.swift").read_text())
    assert "precondition_failed" in verify, (
        "the verifier lost the precondition refusal"
    )
    # Order is the whole property: the check must appear BEFORE the
    # budget spend in the one function that does both.
    body = verify[verify.index("public func verify("):]
    assert body.index(".preconditionFailed") < body.index("budget.spend"), (
        "the precondition check moved after the budget spend; a wired "
        "verb that cannot run is spending a unit again"
    )
    loop = _strip_comments((_BROKER / "Sources/AstraCore/ServeLoop.swift").read_text())
    assert "precondition_failed" in loop, (
        "the broker no longer refuses an impossible intent before the "
        "prompt, so Kunal is asked for a fingerprint that buys nothing"
    )
    grammar = _strip_comments((_BROKER / "Sources/AstraCore/Audit.swift").read_text())
    assert '"precondition_failed"' in grammar, (
        "the reason is outside the closed audit grammar, which HALTS the "
        "daemon the first time it is used"
    )


@requires_broker
def test_the_executors_never_list_names_no_registered_tool():
    """Catalog.never is the executor's compiled list of names that may
    never be verbs. The cloud registry must not carry any of them as a
    tool either: that is the retired bridge's surface, and both ends
    of the pipe must agree it is gone."""
    import astra.runtime.tools  # noqa: F401
    from astra.runtime.tool_registry import REGISTRY, _FORBIDDEN

    never = _swift_table(_CATALOG.read_text(), "never")
    assert never, "Catalog.never parsed empty"
    assert not (never & set(REGISTRY.names())), sorted(never & set(REGISTRY.names()))
    assert not (never & set(client.CATALOGUE_BY_NAME))
    assert {"local_bash", "local_edit", "local_write"} <= (never & _FORBIDDEN)


def test_system_prompt_tables_are_keyed_by_catalogue_verbs():
    from astra.core import system_prompt as sp

    names = set(client.CATALOGUE_BY_NAME)
    assert set(sp._ARG_ORDER) == names, "every verb needs an argument order"
    for verb, order in sp._ARG_ORDER.items():
        assert len(order) == len(set(order)), verb
        assert set(order) == client.CATALOGUE_BY_NAME[verb].arg_keys, verb
    assert set(sp._VERB_NOTES) <= names, set(sp._VERB_NOTES) - names
    assert set(sp._UNWIRED_WHY) <= names, set(sp._UNWIRED_WHY) - names
    prompt = sp.get_system_prompt()
    for spec in client.CATALOGUE:
        assert f"`{spec.name}(" in prompt, f"{spec.name} is not in the prompt"
        line = next(
            ln for ln in prompt.splitlines() if ln.startswith(f"- `{spec.name}(")
        )
        assert ("NOT wired" in line) is (not spec.wired), line
        assert ("fingerprint every time" in line) is spec.signed, line


def test_agent_repos_fix_flow_renders_the_live_wired_state():
    from astra.tools import agent_repos_tools as art

    text = art.fix_flow_text()
    for step, verb in art._FIX_STEPS:
        spec = client.CATALOGUE_BY_NAME[verb]
        line = next(ln for ln in text.splitlines() if f"{step}:" in ln)
        assert ("NOT wired" in line) is (not spec.wired), line
    assert "commit and push: NOT available" in text
    assert not any(v.name.startswith("git.") for v in client.CATALOGUE), (
        "a git verb exists now; the fix flow and the system prompt say "
        "push is unavailable, which is no longer true"
    )


@requires_broker
def test_prompt_quotes_the_compiled_roots_and_read_limits():
    from astra.core.system_prompt import get_system_prompt
    from astra.runtime.tools.physical import _SUBMIT_DESCRIPTION

    prompt = get_system_prompt()
    roots = _swift_table(_CANON.read_text(), "allowedRoots")
    assert roots, "allowedRoots parsed empty"
    for root in roots:
        assert f"`{root}`" in prompt, f"compiled root {root} not in the prompt"
    fv = _strip_comments(_FILEVERBS.read_text())
    limits = {}
    for name in ("defaultReadLimit", "maxReadLimit"):
        m = re.search(rf"{name}\s*=\s*(\d+)\s*\*\s*(\d+)", fv)
        assert m, f"{name} not found"
        limits[name] = int(m.group(1)) * int(m.group(2))
    for text, where in ((prompt, "system prompt"), (_SUBMIT_DESCRIPTION, "submit_intent")):
        assert f"{limits['defaultReadLimit'] // 1024} KiB" in text, (where, "default")
        assert f"{limits['maxReadLimit'] // 1024} KiB" in text, (where, "ceiling")

