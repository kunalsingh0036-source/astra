"""The cloud half of the shared boundary corpus.

Verb arguments are validated twice on purpose: here, cloud-side, so a
refusal is readable and nothing is filed; and on the Mac in
Canonicalizer.swift, which is the gate that actually decides. Both were
written from one spec, both had tests, and they disagreed about the
value `0` for months — Swift read JSON 0 as a boolean (NSNumber bridges
to Bool) and refused it, so the first page of every paged fs.read
failed and WhatsApp export ingestion read zero bytes while unpaged
reads kept working and made the path look healthy.

Neither suite caught it because each chose its own examples. The
examples now live in ONE file that both sides iterate:
astra-broker/Resources/arg_boundary_cases.json. Its Swift half is
SharedBoundaryCorpusTests in Tests/AstraCoreTests/PathPolicyTests.swift.
The cloud repo packages a byte-identical snapshot for standalone CI;
when the Mac repo is present, divergence fails instead of picking a copy.
"""

import json
import pathlib

import pytest

from astra.broker import client

CORPUS = pathlib.Path(__file__).parent / "fixtures" / "arg_boundary_cases.json"
MAC_CORPUS = (pathlib.Path(__file__).resolve().parents[2].parent
          / "astra-broker" / "Resources" / "arg_boundary_cases.json")


def _cases():
    # The fixture is required in CI; absence must FAIL, not skip the gate.
    if MAC_CORPUS.exists():
        assert CORPUS.read_bytes() == MAC_CORPUS.read_bytes(), (
            "The packaged boundary corpus differs from the Mac source. "
            "Update both in the same release; do not bypass this check."
        )
    return json.loads(CORPUS.read_text())["cases"]


def test_the_corpus_is_present_and_has_not_shrunk():
    cases = _cases()
    assert len(cases) > 10, "the shared corpus lost cases"
    # The case that started it all must never be dropped.
    assert any(c["args"].get("offset") == 0 and c["verdict"] == "accept"
               for c in cases), "offset 0 must stay in the corpus"


def test_standalone_checkout_still_runs_the_corpus(monkeypatch, tmp_path):
    monkeypatch.setitem(globals(), "MAC_CORPUS", tmp_path / "absent-mac-repo.json")
    assert len(_cases()) > 10


def test_a_divergent_mac_snapshot_is_refused(monkeypatch, tmp_path):
    divergent = tmp_path / "mac-corpus.json"
    divergent.write_bytes(CORPUS.read_bytes() + b" ")
    monkeypatch.setitem(globals(), "MAC_CORPUS", divergent)
    with pytest.raises(AssertionError, match="differs from the Mac source"):
        _cases()


@pytest.mark.parametrize("case", _cases(), ids=lambda c: f"{c['verb']}:{c['why'][:40]}")
def test_cloud_validator_agrees_with_the_shared_corpus(case):
    spec = client.CATALOGUE_BY_NAME.get(case["verb"])
    assert spec is not None, f"{case['verb']} is not in the mirrored catalogue"
    try:
        client.validate_args(spec, dict(case["args"]))
        accepted, err = True, None
    except Exception as e:                       # noqa: BLE001 - any refusal
        accepted, err = False, str(e)

    if case["verdict"] == "accept":
        assert accepted, (
            f"{case['verb']} {case['args']} must be ACCEPTED "
            f"({case['why']}) — refused with: {err}")
    else:
        assert not accepted, (
            f"{case['verb']} {case['args']} must be REFUSED ({case['why']})")
