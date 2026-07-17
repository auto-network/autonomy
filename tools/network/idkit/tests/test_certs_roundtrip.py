"""Certificate serialization acceptance: canonical round-trip, bit-identical
re-sign, and strict parsing (one accepted byte form for anything signed)."""

from __future__ import annotations

import json

import pytest

from tools.network.idkit import (
    DelegationCert,
    KeyPair,
    MalformedError,
    Subject,
    issue_cert,
)
from tools.network.idkit.certs import MAX_CHAIN_DEPTH

from .conftest import NOW, ORG, SESSION_SCOPE


def test_roundtrip_root_signed_cert_is_bit_identical(chain):
    wire = chain.session_cert.to_json()
    parsed = DelegationCert.from_json(wire)
    assert parsed == chain.session_cert
    assert parsed.to_json() == wire


def test_roundtrip_nested_chain_is_bit_identical(chain):
    wire = chain.agent_cert.to_json()
    parsed = DelegationCert.from_json(wire)
    assert parsed == chain.agent_cert
    assert parsed.to_json() == wire
    assert parsed.parent_cert == chain.session_cert


def test_resign_after_roundtrip_is_bit_identical(chain):
    """Acceptance: deserialize -> re-sign -> identical signature bytes.
    Holds because canonical JSON gives one byte form and Ed25519 signing
    is deterministic."""
    parsed = DelegationCert.from_json(chain.agent_cert.to_json())
    resigned = chain.session_key.sign_hex(parsed.signing_input())
    assert resigned == chain.agent_cert.sig

    parsed_root_hop = parsed.parent_cert
    assert chain.root.sign_hex(parsed_root_hop.signing_input()) == chain.session_cert.sig


def test_non_canonical_input_normalizes_to_canonical_bytes(chain):
    """Reordered keys and whitespace in the incoming JSON must not change
    what the cert *is*: parsing accepts it, re-serialization is canonical."""
    data = json.loads(chain.session_cert.to_json())
    sprawling = json.dumps(dict(reversed(list(data.items()))), indent=2)
    parsed = DelegationCert.from_json(sprawling)
    assert parsed == chain.session_cert
    assert parsed.to_json() == chain.session_cert.to_json()


def test_optional_fields_roundtrip():
    root, child = KeyPair.generate(), KeyPair.generate()
    cert = issue_cert(
        root,
        child.public_hex,
        scope=SESSION_SCOPE,
        org=ORG,
        subject=Subject(kind="persona", id="p-1"),
        not_before=NOW,
        not_after=NOW + 60,
        target_types=("design", "present"),
    )
    parsed = DelegationCert.from_json(cert.to_json())
    assert parsed.target_types == ("design", "present")
    assert parsed.to_json() == cert.to_json()


def _mutate(cert, **changes):
    data = json.loads(cert.to_json())
    for key, value in changes.items():
        if value is _DROP:
            data.pop(key, None)
        else:
            data[key] = value
    return data


_DROP = object()


@pytest.mark.parametrize(
    "changes",
    [
        {"extra_field": 1},  # unknown field
        {"v": 2},  # unsupported version
        {"v": _DROP},
        {"child_pub": _DROP},
        {"sig": _DROP},
        {"scope": _DROP},
        {"org": _DROP},
        {"subject": _DROP},
        {"not_before": _DROP},
        {"not_after": _DROP},
        {"child_pub": "AB" * 32},  # uppercase hex
        {"child_pub": "ab" * 31},  # short hex
        {"sig": "zz" * 64},  # non-hex sig
        {"scope": []},  # empty scope
        {"scope": ["b", "a"]},  # unsorted scope
        {"scope": ["a", "a"]},  # duplicate scope
        {"scope": "link:publish"},  # not a list
        {"scope": ["link:publish", 5]},  # non-string entry
        {"target_types": []},
        {"target_types": ["b", "a"]},
        {"org": ""},
        {"org": 42},
        {"subject": {"kind": "operator"}},  # missing id
        {"subject": {"kind": "wizard", "id": "x"}},  # unknown kind
        {"subject": {"kind": "operator", "id": "x", "extra": 1}},
        {"not_before": "0"},  # string timestamp
        {"not_before": True},  # bool is not an accepted int
        {"not_before": 1.5},  # float
        {"not_after": -1},
        {"not_after": 2**63},
        {"not_before": NOW + 10, "not_after": NOW + 10},  # empty window
        {"not_before": NOW + 20, "not_after": NOW + 10},  # inverted window
        {"parent_cert": "not-an-object"},
    ],
)
def test_strict_parse_rejects_malformed_certs(chain, changes):
    with pytest.raises(MalformedError):
        DelegationCert.from_dict(_mutate(chain.session_cert, **changes))


def test_parse_rejects_non_json_and_wrong_types():
    with pytest.raises(MalformedError):
        DelegationCert.from_json("not json{")
    with pytest.raises(MalformedError):
        DelegationCert.from_json(b"\xff\xfe")
    with pytest.raises(MalformedError):
        DelegationCert.from_dict(["not", "a", "dict"])
    with pytest.raises(MalformedError):
        DelegationCert.from_json(12345)


def test_parse_rejects_over_deep_chains(chain):
    """A hostile blob nesting parent_cert past MAX_CHAIN_DEPTH is rejected
    at parse time, before any signature work."""
    leaf = json.loads(chain.session_cert.to_json())
    data = json.loads(chain.session_cert.to_json())
    for _ in range(MAX_CHAIN_DEPTH):
        data = {**json.loads(chain.session_cert.to_json()), "parent_cert": data}
    assert data != leaf
    with pytest.raises(MalformedError):
        DelegationCert.from_dict(data)


def test_issue_cert_sorts_and_dedupes_scope():
    root, child = KeyPair.generate(), KeyPair.generate()
    cert = issue_cert(
        root,
        child.public_hex,
        scope=["link:revoke", "link:publish", "link:publish"],
        org=ORG,
        subject=Subject(kind="operator", id="op"),
        not_before=NOW,
        not_after=NOW + 60,
    )
    assert cert.scope == ("link:publish", "link:revoke")
