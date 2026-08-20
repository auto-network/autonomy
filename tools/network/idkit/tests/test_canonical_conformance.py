"""Cross-implementation conformance spec for idkit canonical JSON (Track F).

Owned by Test & Automation, on the critical path AHEAD of any second
implementation: these pinned bytes ARE the contract the browser/JS mirror
(crypto Track C) must reproduce byte-for-byte. Two canonicalisers that disagree
on one input produce a signature that verifies on one side and not the other —
worse than either failing — so the safe order is to freeze the vectors first and
build the mirror to pass them, not to diff two implementations after the fact.

Each vector is the exact output of ``canonical_json`` on master. A change to the
canonicaliser (a breaking format change) fails here loudly; a mirror that does
not reproduce a vector is non-conformant. The comments call out the specific trap
a naive ``JSON.stringify`` + ``Object.keys().sort()`` mirror falls into.
"""
from __future__ import annotations

import pytest

from tools.network.idkit import KeyPair
from tools.network.idkit.canonical import canonical_json
from tools.network.idkit.enrollment import ENROLLMENT_DOMAIN, mint, verify
from tools.network.idkit.errors import MalformedError

# name, input, expected canonical bytes, cross-impl trap it pins
CANONICAL_VECTORS = [
    # Large integers: Python emits full precision; a JS mirror using Number /
    # JSON.stringify silently rounds 2**53+1 to ...992 and cannot represent the
    # others at all. A conformant mirror MUST carry integers as bigints/strings.
    ("int_2p53_plus_1", 2 ** 53 + 1, b"9007199254740993"),
    ("int_2p63_minus_1", 2 ** 63 - 1, b"9223372036854775807"),
    ("int_10p30", 10 ** 30, b"1000000000000000000000000000000"),
    # Non-ASCII: ensure_ascii escapes to \uXXXX; a naive JSON.stringify emits the
    # raw UTF-8 character, producing different bytes for the same string.
    ("nonascii_latin1", {"label": "café"}, b'{"label":"caf\\u00e9"}'),
    ("nonascii_bmp", {"x": "Ω"}, b'{"x":"\\u03a9"}'),
    # Astral (>U+FFFF): must be emitted as an escaped UTF-16 surrogate PAIR, and
    # sorted by code point — a JS Array.sort() sorts by UTF-16 code unit, which
    # disagrees for astral-vs-BMP keys in the 0xD800+ range.
    ("astral_value", {"x": "\U0001F600"}, b'{"x":"\\ud83d\\ude00"}'),
    ("astral_key_sort", {"\U0001F600": 1, "a": 2, "Z": 3},
     b'{"Z":3,"a":2,"\\ud83d\\ude00":1}'),
    # Control chars: short escapes for \b\f\n\r\t, \uXXXX for the rest, '/' is
    # NOT escaped, '"' and '\\' are. (Happens to align with JSON.stringify — pin
    # it so a mirror cannot drift, e.g. by escaping '/'.)
    ("controls_and_escapes", {"s": "\x00\x01\t\n\r\"\\/"},
     b'{"s":"\\u0000\\u0001\\t\\n\\r\\"\\\\/"}'),
    # Primitives.
    ("bool_null", {"a": True, "b": False, "n": None},
     b'{"a":true,"b":false,"n":null}'),
    # Key sort is lexicographic by code point: "" < "10" < "9" < "Z" < "a".
    # A mirror that sorts numerically, or locale-aware, diverges.
    ("key_sort", {"b": 1, "a": 2, "Z": 3, "10": 4, "9": 5, "": 6},
     b'{"":6,"10":4,"9":5,"Z":3,"a":2,"b":1}'),
    # Containers: tuples encode as arrays; empties and nesting.
    ("tuple_as_list", {"hlc": (1_755_600_000_000, 0)},
     b'{"hlc":[1755600000000,0]}'),
    ("empties", {"o": {}, "l": []}, b'{"l":[],"o":{}}'),
]

# Inputs a conformant mirror MUST REJECT, not coerce. A JS mirror that accepts a
# float (or emits a Number for one) or a non-string key is a forgery surface.
REJECTED_VECTORS = [
    ("float", {"x": 1.5}),
    ("nan", {"x": float("nan")}),
    ("inf", {"x": float("inf")}),
    ("nonstring_key", {1: "a"}),
]


@pytest.mark.parametrize("name,obj,expected",
                         CANONICAL_VECTORS, ids=[v[0] for v in CANONICAL_VECTORS])
def test_canonical_vector_is_byte_exact(name, obj, expected):
    assert canonical_json(obj) == expected


@pytest.mark.parametrize("name,obj",
                         REJECTED_VECTORS, ids=[v[0] for v in REJECTED_VECTORS])
def test_rejected_vector_raises(name, obj):
    with pytest.raises(MalformedError):
        canonical_json(obj)


def test_enrollment_domain_prefix_is_pinned():
    """The signing_input domain prefix (with its trailing newline) is part of
    the bytes the mirror signs/verifies; pin it so it cannot drift."""
    assert ENROLLMENT_DOMAIN == b"autonomy.identity.passkey-enrollment.v1\n"


def test_enrollment_signing_input_escapes_a_unicode_label():
    """The live cross-impl vector: a device the user named with non-ASCII + an
    astral emoji. The signed bytes must carry the ASCII-escaped form, never the
    raw UTF-8 — so a mirror computing signing_input with a naive JSON.stringify
    would build different bytes and fail to verify a genuine statement."""
    root = KeyPair.generate()
    stmt = mint(
        root=root,
        credential_id="Y3JlZGVudGlhbA",
        credential_public_key="a1" * 20,
        rp_id="localhost",
        origin="https://localhost:8080",
        nonce="ab" * 32,
        created_hlc=(1_755_600_000_000, 0),
        initial_sign_count=7,
        provisioning_public_key="aa" * 32,
        label="café \U0001F512",
    )
    si = stmt.signing_input()
    assert si.startswith(ENROLLMENT_DOMAIN)
    assert b'"label":"caf\\u00e9 \\ud83d\\udd12"' in si  # escaped, ASCII
    assert "café".encode("utf-8") not in si              # never the raw bytes
    assert verify(stmt, root_pub=root.public_hex) is not None
