"""Revocation acceptance: records verify against root; a parent may revoke
its own descendants ONLY; retention is bounded by the revoked key's natural
expiry (invariant I7)."""

from __future__ import annotations

import dataclasses
import json

import pytest

from tools.network.idkit import (
    KeyPair,
    MalformedError,
    NotYetValidError,
    RevocationAuthorityError,
    RevocationError,
    RevocationRecord,
    RevocationSet,
    Subject,
    issue_cert,
    issue_revocation,
    verify_revocation,
)

from .conftest import AGENT_WINDOW, HOUR, NOW, ORG, OTHER_ORG, SESSION_WINDOW, build_chain


def _root_record(chain, key_id=None, **overrides):
    kwargs = dict(
        org=ORG,
        revoked_at=NOW,
        expires_at=AGENT_WINDOW[1],
        reason="compromised",
    )
    kwargs.update(overrides)
    return issue_revocation(chain.root, key_id or chain.agent_key.public_hex, **kwargs)


# --- root-signed records ------------------------------------------------------


def test_root_signed_record_verifies(chain):
    record = _root_record(chain)
    verify_revocation(record, chain.root_pub, org=ORG, revoked_cert=chain.agent_cert)


def test_horizon_proof_is_mandatory_even_for_root(chain):
    """The revoked key's cert is the only trustworthy source of its natural
    not_after — verification without it is refused for every issuer shape."""
    record = _root_record(chain)
    with pytest.raises(RevocationError):
        verify_revocation(record, chain.root_pub, org=ORG)


def test_i7_regression_root_cannot_set_arbitrary_retention(chain):
    """Regression (Codex cross-validation find): a root-signed record with
    expires_at past the revoked key's natural expiry must be rejected BOTH
    with the cert supplied and — critically — when the caller omits it.
    Previously the horizon check was skipped when revoked_cert was None,
    so the record verified OK and purge_expired retained it past the key's
    natural expiry, violating I7."""
    overlong = issue_revocation(
        chain.root,
        chain.agent_key.public_hex,
        org=ORG,
        revoked_at=NOW,
        expires_at=AGENT_WINDOW[1] + 10 * HOUR,
    )
    with pytest.raises(RevocationError):
        verify_revocation(overlong, chain.root_pub, org=ORG)
    with pytest.raises(RevocationError):
        verify_revocation(overlong, chain.root_pub, org=ORG, revoked_cert=chain.agent_cert)


def test_issue_revocation_with_cert_refuses_overlong_horizon(chain):
    """Issuance-time defense in depth mirrors the verification bound."""
    with pytest.raises(MalformedError):
        issue_revocation(
            chain.root,
            chain.agent_key.public_hex,
            org=ORG,
            revoked_at=NOW,
            expires_at=AGENT_WINDOW[1] + 10 * HOUR,
            revoked_cert=chain.agent_cert,
        )
    # and the well-behaved path still mints fine
    record = issue_revocation(
        chain.root,
        chain.agent_key.public_hex,
        org=ORG,
        revoked_at=NOW,
        expires_at=AGENT_WINDOW[1],
        revoked_cert=chain.agent_cert,
    )
    verify_revocation(record, chain.root_pub, org=ORG, revoked_cert=chain.agent_cert)


def test_root_may_revoke_any_key_in_org(chain):
    record = _root_record(chain, key_id=chain.session_key.public_hex, expires_at=SESSION_WINDOW[1])
    verify_revocation(record, chain.root_pub, org=ORG, revoked_cert=chain.session_cert)


def test_record_roundtrips_bit_identically(chain):
    record = _root_record(chain)
    wire = record.to_json()
    parsed = RevocationRecord.from_json(wire)
    assert parsed == record
    assert parsed.to_json() == wire
    assert chain.root.sign_hex(parsed.signing_input()) == record.sig


def test_record_from_json_rejects_noncanonical_wire(chain):
    """Anti-malleability: a record has exactly one accepted wire encoding."""
    record = _root_record(chain)
    pretty = json.dumps(json.loads(record.to_json()), indent=1)
    assert json.loads(pretty) == json.loads(record.to_json())
    with pytest.raises(MalformedError):
        RevocationRecord.from_json(pretty)


def test_rejects_tampered_record(chain):
    record = _root_record(chain)
    tampered = dataclasses.replace(record, revoked_key_id=chain.session_key.public_hex)
    with pytest.raises(RevocationError):
        verify_revocation(tampered, chain.root_pub, org=ORG, revoked_cert=chain.session_cert)


def test_rejects_wrong_org_record(chain):
    record = _root_record(chain, org=OTHER_ORG)
    with pytest.raises(RevocationError):
        verify_revocation(record, chain.root_pub, org=ORG, revoked_cert=chain.agent_cert)


def test_rejects_impersonated_root_issuer(chain):
    """issuer_pub claims root, but the signature was minted by another key."""
    mallory = KeyPair.generate()
    forged = _root_record(chain)
    forged = dataclasses.replace(
        forged, sig=mallory.sign_hex(forged.signing_input())
    )
    with pytest.raises(RevocationError):
        verify_revocation(forged, chain.root_pub, org=ORG, revoked_cert=chain.agent_cert)


def test_root_record_must_not_carry_issuer_cert(chain):
    record = issue_revocation(
        chain.root,
        chain.agent_key.public_hex,
        org=ORG,
        revoked_at=NOW,
        expires_at=AGENT_WINDOW[1],
        issuer_cert=None,
    )
    smuggled = dataclasses.replace(record, issuer_cert=chain.session_cert)
    smuggled = dataclasses.replace(smuggled, sig=chain.root.sign_hex(smuggled.signing_input()))
    with pytest.raises(RevocationError):
        verify_revocation(smuggled, chain.root_pub, org=ORG, revoked_cert=chain.agent_cert)


# --- parent-signed records: descendants only ------------------------------------


def _session_revokes(chain, revoked_key_id, **overrides):
    kwargs = dict(
        org=ORG,
        revoked_at=NOW,
        expires_at=AGENT_WINDOW[1],
        issuer_cert=chain.session_cert,
    )
    kwargs.update(overrides)
    return issue_revocation(chain.session_key, revoked_key_id, **kwargs)


def test_parent_revokes_its_own_descendant(chain):
    record = _session_revokes(chain, chain.agent_key.public_hex)
    verify_revocation(record, chain.root_pub, org=ORG, revoked_cert=chain.agent_cert)


def test_grandparent_style_ancestor_revokes_deeper_descendant(chain):
    mid_key, leaf_key = KeyPair.generate(), KeyPair.generate()
    mid = issue_cert(
        chain.session_key,
        mid_key.public_hex,
        scope=("link:publish", "link:revoke"),
        org=ORG,
        subject=Subject(kind="agent", id="mid"),
        not_before=NOW - 10,
        not_after=NOW + HOUR,
        parent_cert=chain.session_cert,
    )
    leaf = issue_cert(
        mid_key,
        leaf_key.public_hex,
        scope=("link:publish",),
        org=ORG,
        subject=Subject(kind="persona", id="leaf"),
        not_before=NOW,
        not_after=NOW + 60,
        parent_cert=mid,
    )
    record = _session_revokes(chain, leaf_key.public_hex, expires_at=NOW + 60)
    verify_revocation(record, chain.root_pub, org=ORG, revoked_cert=leaf)


def test_parent_cannot_revoke_non_descendant(chain):
    """A sibling session's agent is NOT a descendant — authority denied even
    though every signature involved is genuine."""
    # A second session under the SAME org root, so descent is the only
    # thing distinguishing it from the issuer's own subtree.
    other_session_key = KeyPair.generate()
    other_session_cert = issue_cert(
        chain.root,
        other_session_key.public_hex,
        scope=("delegate:agent", "link:publish", "link:revoke"),
        org=ORG,
        subject=Subject(kind="operator", id="operator-session-2"),
        not_before=SESSION_WINDOW[0],
        not_after=SESSION_WINDOW[1],
    )
    other_agent_key = KeyPair.generate()
    other_agent_cert = issue_cert(
        other_session_key,
        other_agent_key.public_hex,
        scope=("link:publish",),
        org=ORG,
        subject=Subject(kind="agent", id="agent-session-2"),
        not_before=AGENT_WINDOW[0],
        not_after=AGENT_WINDOW[1],
        parent_cert=other_session_cert,
    )
    record = _session_revokes(chain, other_agent_key.public_hex)
    with pytest.raises(RevocationAuthorityError):
        verify_revocation(record, chain.root_pub, org=ORG, revoked_cert=other_agent_cert)


def test_parent_cannot_revoke_itself(chain):
    record = _session_revokes(chain, chain.session_key.public_hex, expires_at=SESSION_WINDOW[1])
    with pytest.raises(RevocationAuthorityError):
        verify_revocation(record, chain.root_pub, org=ORG, revoked_cert=chain.session_cert)


def test_delegated_record_requires_issuer_cert(chain):
    record = issue_revocation(
        chain.session_key,
        chain.agent_key.public_hex,
        org=ORG,
        revoked_at=NOW,
        expires_at=AGENT_WINDOW[1],
        issuer_cert=None,  # forgot to attach authority proof
    )
    with pytest.raises(RevocationAuthorityError):
        verify_revocation(record, chain.root_pub, org=ORG, revoked_cert=chain.agent_cert)


def test_delegated_record_requires_revoked_cert_descent_proof(chain):
    """Without the revoked key's cert there is neither an expiry-horizon
    proof nor a descent proof — refused at the universal requirement."""
    record = _session_revokes(chain, chain.agent_key.public_hex)
    with pytest.raises(RevocationError):
        verify_revocation(record, chain.root_pub, org=ORG, revoked_cert=None)


def test_delegated_record_rejects_mismatched_issuer_cert(chain):
    record = _session_revokes(chain, chain.agent_key.public_hex)
    doctored = dataclasses.replace(record, issuer_cert=chain.agent_cert)
    doctored = dataclasses.replace(doctored, sig=chain.session_key.sign_hex(doctored.signing_input()))
    with pytest.raises(RevocationAuthorityError):
        verify_revocation(doctored, chain.root_pub, org=ORG, revoked_cert=chain.agent_cert)


def test_delegated_record_rejects_backdated_issuer_authority(chain):
    """The issuer chain is checked as of revoked_at — a revocation dated
    before the issuer key even existed is refused. (The forward direction —
    revoked_at after issuer expiry — is unreachable for genuine chains:
    strict window nesting means every descendant, and hence the record's
    bounded expires_at, dies before the issuer does.)"""
    record = _session_revokes(
        chain,
        chain.agent_key.public_hex,
        revoked_at=SESSION_WINDOW[0] - 10,
        expires_at=NOW,
    )
    with pytest.raises(NotYetValidError):
        verify_revocation(record, chain.root_pub, org=ORG, revoked_cert=chain.agent_cert)


def test_rejects_revoked_cert_leaf_mismatch(chain):
    record = _session_revokes(chain, chain.agent_key.public_hex)
    with pytest.raises(RevocationError):
        verify_revocation(record, chain.root_pub, org=ORG, revoked_cert=chain.session_cert)


# --- I7: bounded retention -------------------------------------------------------


def test_rejects_expires_at_beyond_natural_expiry(chain):
    record = _root_record(chain, expires_at=AGENT_WINDOW[1] + HOUR)
    with pytest.raises(RevocationError):
        verify_revocation(record, chain.root_pub, org=ORG, revoked_cert=chain.agent_cert)


def test_record_always_carries_a_ttl():
    """expires_at is a required field with a sane relation to revoked_at —
    unbounded retention is unrepresentable."""
    chain = build_chain()
    with pytest.raises(MalformedError):
        issue_revocation(
            chain.root,
            chain.agent_key.public_hex,
            org=ORG,
            revoked_at=NOW,
            expires_at=NOW,  # zero-length retention
        )
    data = json.loads(_root_record(chain).to_json())
    del data["expires_at"]
    with pytest.raises(MalformedError):
        RevocationRecord.from_dict(data)


def test_revocation_set_purges_expired_records(chain):
    record = _root_record(chain)  # expires_at = AGENT_WINDOW[1]
    live = _root_record(chain, key_id=chain.session_key.public_hex, expires_at=SESSION_WINDOW[1])
    rset = RevocationSet()
    rset.add(record)
    rset.add(live)
    assert rset.is_revoked(chain.agent_key.public_hex)
    assert rset.is_revoked(chain.session_key.public_hex)
    assert len(rset) == 2

    purged = rset.purge_expired(now=AGENT_WINDOW[1] + 1)
    assert purged == 1
    assert not rset.is_revoked(chain.agent_key.public_hex)  # key is past its own expiry anyway
    assert rset.is_revoked(chain.session_key.public_hex)
    assert len(rset) == 1


def test_revocation_set_keeps_longest_horizon(chain):
    early = _root_record(chain, expires_at=NOW + 10)
    late = _root_record(chain, expires_at=AGENT_WINDOW[1])
    rset = RevocationSet()
    rset.add(late)
    rset.add(early)  # must not shorten the retained horizon
    assert rset.get(chain.agent_key.public_hex).expires_at == AGENT_WINDOW[1]


def test_revocation_set_rejects_raw_ids():
    rset = RevocationSet()
    with pytest.raises(MalformedError):
        rset.add("ab" * 32)


# --- strict parsing ---------------------------------------------------------------


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(extra=1),
        lambda d: d.update(v=2),
        lambda d: d.pop("sig"),
        lambda d: d.pop("issuer_pub"),
        lambda d: d.pop("revoked_key_id"),
        lambda d: d.update(revoked_key_id="AB" * 32),
        lambda d: d.update(revoked_at="soon"),
        lambda d: d.update(expires_at=True),
        lambda d: d.update(reason=42),
        lambda d: d.update(issuer_cert="yes"),
    ],
)
def test_strict_parse_rejects_malformed_records(chain, mutate):
    data = json.loads(_root_record(chain).to_json())
    mutate(data)
    with pytest.raises(MalformedError):
        RevocationRecord.from_dict(data)
