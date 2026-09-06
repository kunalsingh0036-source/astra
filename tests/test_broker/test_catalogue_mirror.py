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

requires_broker = pytest.mark.skipif(
    not all(p.is_file() for p in (_CATALOG, _EXECUTOR, _CANON, _FILEVERBS, _RATELIMIT)),
    reason=f"astra-broker not checked out beside astra at {_BROKER}; the "
           "catalogue mirror is unpinned in this checkout",
)


def _strip_comments(src: str) -> str:
    return re.sub(r"//[^\n]*", "", src)


def _strings(swift_list: str) -> frozenset[str]:
    return frozenset(re.findall(r'"([^"]+)"', swift_list))


def _parse_catalog(src: str) -> list[dict]:
    src = _strip_comments(src)
    pat = re.compile(
        r'VerbSpec\(\s*id:\s*(\d+),\s*name:\s*"([^"]+)",\s*policy:\s*\.(\w+),'
        r'\s*degradation:\s*\.(\w+),\s*maxArgBytes:\s*([\d_]+),'
        r'\s*irreversible:\s*(true|false),\s*runsInJail:\s*(true|false),'
        r'\s*argKeys:\s*\[([^\]]*)\],\s*requiredKeys:\s*\[([^\]]*)\],'
        r'\s*pathKeys:\s*\[([^\]]*)\],\s*patternKeys:\s*\[([^\]]*)\],'
        r'\s*intKeys:\s*\[([^\]]*)\]\s*\)', re.S,
    )
    out = []
    for m in pat.finditer(src):
        out.append({
            "id": int(m.group(1)), "name": m.group(2), "policy": m.group(3),
            "degradation": m.group(4),
            "max_arg_bytes": int(m.group(5).replace("_", "")),
            "arg_keys": _strings(m.group(8)),
            "required_keys": _strings(m.group(9)),
            "path_keys": _strings(m.group(10)),
            "pattern_keys": _strings(m.group(11)),
            "int_keys": _strings(m.group(12)),
        })
    return out


def _parse_executor_cases(src: str) -> frozenset[str]:
    """The executor's wired set. Two forms have existed: a
    `let wired: [String: VerbImpl] = [...]` table (current; its keys
    are also what the executor reports in every challenge reply and
    what the Verifier refuses outside of), and before that `case
    "verb":` labels inside execute(). The table wins when present."""
    src = _strip_comments(src)
    m = re.search(
        r"let wired:\s*\[String:\s*VerbImpl\]\s*=\s*\[(.*?)\n\]", src, re.S,
    )
    if m:
        return frozenset(re.findall(r'"([\w.]+)"\s*:', m.group(1)))
    start = src.index("func execute(")
    end = src.index("func executeBounded(")
    body = src[start:end]
    return frozenset(re.findall(r'case\s+"([\w.]+)"\s*:', body))


def test_executor_parser_reads_both_forms():
    table = 'let wired: [String: VerbImpl] = [\n    "fs.read": fsRead,\n    "fs.glob": fsGlob,\n]\n'
    assert _parse_executor_cases(table) == {"fs.read", "fs.glob"}
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
    assert len(swift) == 8, "Catalog.swift parse found the wrong number of verbs"
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
    assert dict(read.int_max) == {"limit": client.FS_READ_MAX_LIMIT}


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
def test_executor_still_refuses_unwired_verbs_honestly():
    src = _EXECUTOR.read_text()
    assert "not wired" in src, (
        "the executor lost its honest refusal for an unwired verb "
        "(CHARTER §8: nothing silently no-ops)"
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

