"""Witness wire format v2: grouped, timestamped, version-dispatched (auto-jqd9q).

Section 1 of the bead — the wire format only. The server chain and the client
verifier land separately; this pins the entry shape, the domain separation, and
the version dispatch they both build on.
"""

from __future__ import annotations

import pytest

from tools.network.idkit import KeyPair
from tools.network.registry import witness as w

ORG = "org-uuid-1"
PUB = "ab" * 32
H1 = "11" * 32
H2 = "22" * 32
H3 = "33" * 32


def test_build_entry_v2_is_canonical_and_grouped():
    e = w.build_entry_v2(ORG, 1, {"authority": [H2, H1], "storage": [H3]}, None, 1000, PUB)
    assert e["v"] == 2
    assert e["heads"] == {"authority": [H1, H2], "storage": [H3]}  # sorted, per-topic
    assert e["t"] == 1000 and e["prev"] is None and e["seq"] == 1


def test_one_topic_is_enough():
    e = w.build_entry_v2(ORG, 1, {"authority": [H1]}, None, 5, PUB)
    assert set(e["heads"]) == {"authority"}


def test_unknown_topic_is_refused():
    with pytest.raises(w.WitnessFormatError):
        w.build_entry_v2(ORG, 1, {"ledger": [H1]}, None, 5, PUB)


def test_empty_head_map_is_refused():
    with pytest.raises(w.WitnessFormatError):
        w.build_entry_v2(ORG, 1, {}, None, 5, PUB)


def test_negative_or_nonint_t_is_refused():
    with pytest.raises(w.WitnessFormatError):
        w.build_entry_v2(ORG, 1, {"authority": [H1]}, None, -1, PUB)
    with pytest.raises(w.WitnessFormatError):
        w.build_entry_v2(ORG, 1, {"authority": [H1]}, None, True, PUB)  # bool is not an int t


def test_prev_null_iff_seq_one():
    with pytest.raises(w.WitnessFormatError):
        w.build_entry_v2(ORG, 2, {"authority": [H1]}, None, 5, PUB)  # seq 2 needs prev
    with pytest.raises(w.WitnessFormatError):
        w.build_entry_v2(ORG, 1, {"authority": [H1]}, H2, 5, PUB)  # seq 1 forbids prev


def test_validate_entry_v2_roundtrips_build():
    e = w.build_entry_v2(ORG, 2, {"authority": [H1], "storage": [H2]}, H3, 42, PUB)
    assert w.validate_entry_v2(e) == e


def test_validate_entry_v2_rejects_unsorted_heads():
    e = w.build_entry_v2(ORG, 1, {"authority": [H1, H2]}, None, 5, PUB)
    e = dict(e, heads={"authority": [H2, H1]})  # de-canonicalize
    with pytest.raises(w.WitnessFormatError):
        w.validate_entry_v2(e)


def test_validate_entry_v2_rejects_extra_field():
    e = w.build_entry_v2(ORG, 1, {"authority": [H1]}, None, 5, PUB)
    with pytest.raises(w.WitnessFormatError):
        w.validate_entry_v2(dict(e, extra=1))


def test_v1_shaped_entry_fails_v2_validation():
    v1 = w.build_entry(ORG, "ledger", 1, [H1], None, PUB)
    with pytest.raises(w.WitnessFormatError):
        w.validate_entry_v2(v1)


def test_sign_and_verify_v2_roundtrip():
    key = KeyPair.generate()
    e = w.build_entry_v2(ORG, 1, {"authority": [H1], "storage": [H2]}, None, 7, PUB)
    att = w.sign_attestation_v2(key, e)
    assert w.verify_attestation_v2(att, key.public_hex) == e


def test_v2_signature_domain_is_separate_from_v1():
    """A v2 entry signs under the v2 domain, so its signature bytes cover
    different input than the same-looking v1 entry would — the domains cannot
    be confused."""
    key = KeyPair.generate()
    e2 = w.build_entry_v2(ORG, 1, {"authority": [H1]}, None, 7, PUB)
    assert w.attestation_signing_input(e2).startswith(w.WITNESS_DOMAIN_V2)
    v1 = w.build_entry(ORG, "ledger", 1, [H1], None, PUB)
    assert w.attestation_signing_input(v1).startswith(w.WITNESS_DOMAIN)


def test_verify_v2_refuses_a_v1_attestation():
    key = KeyPair.generate()
    v1 = w.build_entry(ORG, "ledger", 1, [H1], None, PUB)
    att = w.sign_attestation(key, v1)
    with pytest.raises(w.WitnessFormatError):
        w.verify_attestation_v2(att, key.public_hex)


def test_validate_any_dispatches_by_shape():
    v1 = w.build_entry(ORG, "ledger", 1, [H1], None, PUB)
    v2 = w.build_entry_v2(ORG, 1, {"authority": [H1]}, None, 7, PUB)
    assert w.validate_any_entry(v1) == v1
    assert w.validate_any_entry(v2) == v2
    with pytest.raises(w.WitnessFormatError):
        w.validate_any_entry({"v": 3, "org": ORG})  # unknown version fails closed
