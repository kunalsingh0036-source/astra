"""
ACE-1 — Astra Canonical Encoding v1.

The intent digest that Kunal's fingerprint signs is
`SHA256(ACE1(canonicalise(verb, raw_args)))`. Two independent programs
compute it — the broker (Swift) before asking for the signature, and the
executor (Swift) before acting — and they must agree byte for byte or
the executor refuses. This module is the **test oracle**: it proves the
Swift implementation is right and is never in the trust path.

    value := tag(u8) ‖ payload
      0x01 null
      0x02 false            0x03 true
      0x04 int     ‖ i64 big-endian (8 bytes)
      0x05 string  ‖ u32 BE byte-length ‖ NFC-normalised UTF-8
      0x06 bytes   ‖ u32 BE length ‖ raw
      0x07 array   ‖ u32 BE count ‖ value*
      0x08 map     ‖ u32 BE count ‖ (u32 BE keylen ‖ NFC-UTF-8 key ‖ value)*
                     keys sorted by unsigned lexicographic order of the
                     NFC-UTF-8 key bytes

Three rules carry the security weight:

**There is no float tag.** `0.1`, `1e-1` and
`0.1000000000000000055511151231257827` are one JSON number and three
different intents. A float raises `NotCanonical` — a validation failure,
not a serialisation problem.

**Strings are NFC-normalised.** `café` composed and `café` decomposed are
the same path on disk but different bytes on the wire. Without
normalisation an attacker signs one and executes the other.

**Nothing is ever truncated.** Truncation is how you get someone to
approve `/prod.db` believing it was `/pro.db`. Oversized input is
refused, with a message saying so. (This is the explicit fix for the
class found in `astra/autonomy/approvals.py`, where slicing serialised
JSON produced invalid JSON and made large calls silently unapprovable.)
"""

from __future__ import annotations

import hashlib
import struct
import unicodedata
from typing import Any

__all__ = ["NotCanonical", "encode", "digest", "TAG"]

INT64_MIN = -(2 ** 63)
INT64_MAX = 2 ** 63 - 1

TAG = {
    "null": 0x01,
    "false": 0x02,
    "true": 0x03,
    "int": 0x04,
    "string": 0x05,
    "bytes": 0x06,
    "array": 0x07,
    "map": 0x08,
}


class NotCanonical(ValueError):
    """The value cannot be canonically encoded, so it cannot be signed.

    Always a refusal, never a best-effort fallback: a value we cannot
    encode identically in two languages is a value the executor and the
    broker could disagree about, and disagreement is exactly what the
    digest exists to prevent.
    """


def _u32(n: int) -> bytes:
    if n < 0 or n > 0xFFFF_FFFF:
        raise NotCanonical(f"length {n} does not fit in u32")
    return struct.pack(">I", n)


def _nfc(s: str, *, what: str) -> bytes:
    if not isinstance(s, str):
        raise NotCanonical(f"{what} must be a string, got {type(s).__name__}")
    return unicodedata.normalize("NFC", s).encode("utf-8")


def encode(value: Any) -> bytes:
    """Encode `value` to its ACE-1 byte string, or raise NotCanonical."""
    out = bytearray()
    _encode_into(value, out)
    return bytes(out)


def digest(value: Any) -> bytes:
    """SHA-256 over the ACE-1 encoding. 32 bytes."""
    return hashlib.sha256(encode(value)).digest()


def _encode_into(v: Any, out: bytearray) -> None:
    # bool BEFORE int: in Python bool is a subclass of int, so an
    # isinstance(v, int) check would swallow True/False and encode them
    # as 1/0 — silently changing the meaning of an argument.
    if v is None:
        out.append(TAG["null"])
        return
    if v is True:
        out.append(TAG["true"])
        return
    if v is False:
        out.append(TAG["false"])
        return
    if isinstance(v, float):
        raise NotCanonical(
            f"float {v!r} is not canonically encodable — floats have "
            "multiple exact representations of the same JSON number, so "
            "the broker and the executor could sign and execute "
            "different values. Send an integer, or a decimal string."
        )
    if isinstance(v, int):
        if v < INT64_MIN or v > INT64_MAX:
            raise NotCanonical(
                f"integer {v} is outside int64 [{INT64_MIN}, {INT64_MAX}]"
            )
        out.append(TAG["int"])
        out.extend(struct.pack(">q", v))
        return
    if isinstance(v, str):
        b = _nfc(v, what="string")
        out.append(TAG["string"])
        out.extend(_u32(len(b)))
        out.extend(b)
        return
    if isinstance(v, (bytes, bytearray)):
        out.append(TAG["bytes"])
        out.extend(_u32(len(v)))
        out.extend(bytes(v))
        return
    if isinstance(v, (list, tuple)):
        out.append(TAG["array"])
        out.extend(_u32(len(v)))
        for item in v:
            _encode_into(item, out)
        return
    if isinstance(v, dict):
        # Normalise keys FIRST, then check for collisions. Two keys that
        # differ only by Unicode composition are the same key after NFC,
        # and silently keeping one would let an attacker smuggle a
        # second value past a human reading the approval text.
        pairs: list[tuple[bytes, Any]] = []
        seen: dict[bytes, str] = {}
        for k, val in v.items():
            kb = _nfc(k, what="map key")
            if kb in seen:
                raise NotCanonical(
                    f"duplicate map key after NFC normalisation: "
                    f"{seen[kb]!r} and {k!r} both normalise to the same "
                    "bytes"
                )
            seen[kb] = k
            pairs.append((kb, val))
        pairs.sort(key=lambda kv: kv[0])
        out.append(TAG["map"])
        out.extend(_u32(len(pairs)))
        for kb, val in pairs:
            out.extend(_u32(len(kb)))
            out.extend(kb)
            _encode_into(val, out)
        return
    raise NotCanonical(
        f"type {type(v).__name__} has no ACE-1 encoding — the encodable "
        "set is deliberately closed"
    )
