"""Cross-language contract tests for ACE-1 (Python half).

This suite and astra-broker/Tests/AstraACETests/ACETests.swift read the
SAME file — astra-broker/Resources/ace_vectors.json. If Swift and
Python ever disagree about a single vector, both suites go red on the
same name. Neither side may edit an expectation to make its own tests
pass, because the other side asserts the identical value.

Why it matters more than a normal serialiser test: the broker encodes
the arguments Kunal's fingerprint signs, and the executor independently
re-encodes what it was handed. A one-byte disagreement either refuses
valid work forever, or — if the disagreement is exploitable rather than
random — yields a signature over A and execution of B.
"""

from __future__ import annotations

import hashlib

import pytest

from astra.broker.ace import NotCanonical, digest, encode
from astra.broker.vectors import build, load

pytestmark = pytest.mark.filterwarnings("ignore")


def _vectors():
    try:
        return load()
    except FileNotFoundError as e:      # astra-broker not checked out
        pytest.skip(str(e))


# ── the shared contract ─────────────────────────────────────


def test_vector_file_is_ace1():
    assert _vectors()["spec"] == "ACE-1"


def test_encode_vectors_match_swift():
    vf = _vectors()
    assert vf["encode"], "vector file has no encode cases"
    for case in vf["encode"]:
        value = build(case["value"])
        b = encode(value)
        assert len(b) == case["len"], (
            f"{case['name']}: byte length differs from Swift"
        )
        assert hashlib.sha256(b).hexdigest() == case["sha256"], (
            f"{case['name']}: DIGEST DIFFERS FROM SWIFT"
        )
        if "hex" in case:
            assert b.hex() == case["hex"], (
                f"{case['name']}: bytes differ from Swift"
            )


def test_reject_vectors_are_refused():
    vf = _vectors()
    assert vf["reject"], "vector file has no reject cases"
    for case in vf["reject"]:
        with pytest.raises(NotCanonical):
            encode(build(case["value"]))


# ── properties the vector file cannot express ───────────────


def test_bool_is_not_encoded_as_int():
    """In Python `bool` is a subclass of `int`, so an isinstance(v, int)
    check placed first would encode True as 1 — silently changing the
    meaning of an argument, and disagreeing with Swift where Bool and
    Int64 are unrelated types."""
    assert encode(True) == bytes([0x03])
    assert encode(False) == bytes([0x02])
    assert encode(1) == bytes([0x04, 0, 0, 0, 0, 0, 0, 0, 1])
    assert encode(True) != encode(1)


def test_map_keys_sort_by_raw_byte_not_codepoint():
    """ACE-1 requires unsigned lexicographic order over NFC-UTF-8 bytes,
    so "Z" (0x5A) precedes "a" (0x61)."""
    b = encode({"a": 1, "Z": 2})
    assert b[0] == 0x08                      # map tag
    assert b[9:10] == b"Z"                   # tag+count+keylen = 9 bytes


def test_nfd_and_nfc_produce_identical_bytes():
    nfc = "café"
    nfd = "café"
    assert nfc != nfd, "the two inputs must genuinely differ"
    assert encode(nfc) == encode(nfd)
    assert encode({nfc: 1}) == encode({nfd: 1})


def test_duplicate_key_after_nfc_raises():
    """THE CROSS-LANGUAGE TRAP (found 2026-08-30). Python's dict compares
    keys by codepoint, so both survive to the encoder and it raises.
    SWIFT'S DOES NOT — Swift string equality is Unicode canonical
    equivalence, so the two collapse at dictionary insertion, before any
    encoder runs, silently keeping the last value.

    That is why the Swift side enforces this rule in
    `ACE.map(pairs:)` at the JSON→Val boundary, where the ordered pairs
    still exist, and NOT in its encoder. Same bytes out for every valid
    input; opposite behaviour on the one input that must be refused.
    Anyone porting ACE-1 to a third language must check which semantics
    that language's map has before deciding where this guard lives."""
    with pytest.raises(NotCanonical, match="duplicate map key"):
        encode({"café": 1, "café": 2})


def test_floats_are_refused_everywhere_in_the_tree():
    with pytest.raises(NotCanonical, match="float"):
        encode(0.1)
    with pytest.raises(NotCanonical, match="float"):
        encode({"timeout": 1.5})
    with pytest.raises(NotCanonical, match="float"):
        encode([1, 2, 3.0])


def test_int64_boundaries():
    assert len(encode(-(2 ** 63))) == 9
    assert len(encode(2 ** 63 - 1)) == 9
    with pytest.raises(NotCanonical, match="int64"):
        encode(2 ** 63)
    with pytest.raises(NotCanonical, match="int64"):
        encode(-(2 ** 63) - 1)


def test_nothing_is_truncated():
    big = "x" * 1_048_576
    assert len(encode(big)) == 1 + 4 + 1_048_576


def test_unknown_type_is_refused_not_coerced():
    class Weird:
        pass

    with pytest.raises(NotCanonical, match="closed"):
        encode(Weird())
    with pytest.raises(NotCanonical):
        encode({1: "int key"})          # keys must be strings


def test_digest_is_sha256_of_encoding():
    v = {"verb": "fs.read", "path": "/tmp/x"}
    assert digest(v) == hashlib.sha256(encode(v)).digest()
    assert len(digest(v)) == 32
