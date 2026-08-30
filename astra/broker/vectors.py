"""Shared ACE-1 test-vector decoding.

The vector file `astra-broker/Resources/ace_vectors.json` is read by
BOTH test suites — Python here and Swift in AstraACETests. If the two
languages ever disagree about a single vector, both suites go red on
the same line, which is the only way a cross-language encoding stays
honest over time.

JSON cannot express the ACE-1 type set on its own (it has one number
type, no bytes, and no way to pin key order), so every value in the
file is a tagged envelope:

    {"t":"null"}
    {"t":"bool",  "v": true}
    {"t":"int",   "v": "-9223372036854775808"}   decimal STRING, so the
                                                 value survives JSON
    {"t":"str",   "v": "caf\\u0065\\u0301"}       \\u escapes let a vector
                                                 pin NFD vs NFC exactly
    {"t":"bytes", "v": "deadbeef"}               hex
    {"t":"arr",   "v": [ ... ]}
    {"t":"map",   "v": [["k", <val>], ...]}      ARRAY of pairs, so a
                                                 vector can ship keys
                                                 deliberately unsorted
    {"t":"float", "v": 0.1}                      only ever in `reject`
    {"t":"repeat","ch":"x","n":1048576}          large values without a
                                                 large vector file
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

VECTORS_PATH = (
    pathlib.Path(__file__).resolve().parents[2].parent
    / "astra-broker/Resources/ace_vectors.json"
)


class BadVector(ValueError):
    pass


def build(node: Any) -> Any:
    """Turn a tagged envelope into the native value to encode."""
    if not isinstance(node, dict) or "t" not in node:
        raise BadVector(f"not a tagged value: {node!r}")
    t = node["t"]
    if t == "null":
        return None
    if t == "bool":
        return bool(node["v"])
    if t == "int":
        return int(node["v"])          # decimal string -> unbounded int
    if t == "str":
        return node["v"]
    if t == "bytes":
        return bytes.fromhex(node["v"])
    if t == "float":
        return float(node["v"])
    if t == "repeat":
        return str(node["ch"]) * int(node["n"])
    if t == "arr":
        return [build(x) for x in node["v"]]
    if t == "map":
        out: dict[str, Any] = {}
        for pair in node["v"]:
            k, v = pair[0], pair[1]
            if k in out:
                # A literal duplicate in the FILE would be silently
                # collapsed by dict; the encoder's duplicate rule is
                # about post-NFC collisions, which this preserves.
                raise BadVector(f"vector has a literal duplicate key {k!r}")
            out[k] = build(v)
        return out
    raise BadVector(f"unknown vector tag {t!r}")


def load() -> dict[str, Any]:
    if not VECTORS_PATH.exists():
        raise FileNotFoundError(
            f"ACE-1 vector file missing at {VECTORS_PATH} — the Swift and "
            "Python suites read the SAME file; without it neither side "
            "proves anything about the other."
        )
    return json.loads(VECTORS_PATH.read_text())
