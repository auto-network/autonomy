"""Chain-verification acceptance: a valid root->session->agent chain passes;
expired hops, scope escalation, revoked keys, wrong-org certs, and tampered
signatures are all rejected (invariants I4, I7; spec §3)."""

from __future__ import annotations

import dataclasses

import pytest

from tools.network.idkit import (
    ExpiredError,
    KeyPair,
    MalformedError,
    NotYetValidError,
    RevokedError,
    ScopeError,
    ScopeEscalationError,
    SignatureError,
    Subject,
    TTLViolationError,
    WrongOrgError,
    issue_cert,
    verify_chain,
)
from tools.network.idkit.certs import MAX_CHAIN_DEPTH

from .conftest import (
    AGENT_WINDOW,
    HOUR,
    NOW,
    ORG,
    OTHER_ORG,
    SESSION_WINDOW,
    build_chain,
    force_sign,
)


# --- acceptance: the happy path ---------------------------------------------


def test_valid_root_session_agent_chain_verifies(chain):
    result = verify_chain(chain.agent_cert, chain.root_pub, org=ORG, now=NOW)
    assert result.leaf_pub == chain.agent_key.public_hex
    assert result.scope == ("link:publish",)
    assert result.subject_kind == "agent"
    assert result.subject_id == "agent-session-1"
    assert result.org == ORG
    assert result.depth == 2
    assert result.not_after == AGENT_WINDOW[1]


def test_single_hop_root_signed_cert_verifies(chain):
    result = verify_chain(chain.session_cert, chain.root_pub, org=ORG, now=NOW)
    assert result.leaf_pub == chain.session_key.public_hex
    assert result.depth == 1


def test_three_hop_chain_verifies(chain):
    # The fixture agent's scope is a single entry, so strict narrowing
    # forbids a further hop below it; build the 3-hop chain off a wider
    # mid-tier instead.
    leaf_key = KeyPair.generate()
    mid_key = KeyPair.generate()
    mid_cert = issue_cert(
        chain.session_key,
        mid_key.public_hex,
        scope=("link:publish", "link:revoke"),
        org=ORG,
        subject=Subject(kind="agent", id="agent-wide"),
        not_before=NOW - 10,
        not_after=NOW + HOUR,
        parent_cert=chain.session_cert,
    )
    leaf_cert = issue_cert(
        mid_key,
        leaf_key.public_hex,
        scope=("link:publish",),
        org=ORG,
        subject=Subject(kind="persona", id="viewer-1"),
        not_before=NOW,
        not_after=NOW + 60,
        parent_cert=mid_cert,
    )
    result = verify_chain(leaf_cert, chain.root_pub, org=ORG, now=NOW)
    assert result.depth == 3
    assert result.leaf_pub == leaf_key.public_hex


def test_required_scope_and_target_type(chain):
    verify_chain(chain.agent_cert, chain.root_pub, org=ORG, now=NOW, required_scope="link:publish")
    with pytest.raises(ScopeError):
        verify_chain(chain.agent_cert, chain.root_pub, org=ORG, now=NOW, required_scope="link:revoke")
    # leaf with no target_types restriction accepts any required target type
    verify_chain(chain.agent_cert, chain.root_pub, org=ORG, now=NOW, required_target_type="present")


# --- acceptance: expired hop -------------------------------------------------


def test_rejects_expired_leaf_hop(chain):
    with pytest.raises(ExpiredError):
        verify_chain(chain.agent_cert, chain.root_pub, org=ORG, now=AGENT_WINDOW[1] + 1)


def test_rejects_expired_root_signed_hop(chain):
    with pytest.raises(ExpiredError):
        verify_chain(chain.session_cert, chain.root_pub, org=ORG, now=SESSION_WINDOW[1] + 1)


def test_rejects_not_yet_valid_hop(chain):
    with pytest.raises(NotYetValidError):
        verify_chain(chain.agent_cert, chain.root_pub, org=ORG, now=AGENT_WINDOW[0] - 1)


def test_expired_ancestor_kills_chain_even_if_leaf_claims_validity(chain):
    """Forge a leaf whose window escapes its parent's; at a time past the
    parent's expiry the chain must still die (on the ancestor's expiry or
    the window violation — either way, rejected)."""
    rogue_key = KeyPair.generate()
    rogue = force_sign(
        chain.session_key,
        child_pub=rogue_key.public_hex,
        scope=("link:publish",),
        org=ORG,
        subject=Subject(kind="agent", id="rogue"),
        not_before=NOW,
        not_after=SESSION_WINDOW[1] + HOUR,  # outlives parent
        parent_cert=chain.session_cert,
    )
    with pytest.raises((ExpiredError, TTLViolationError)):
        verify_chain(rogue, chain.root_pub, org=ORG, now=SESSION_WINDOW[1] + 10)


# --- acceptance: scope-escalating hop ----------------------------------------


def test_rejects_scope_escalating_hop(chain):
    rogue_key = KeyPair.generate()
    rogue = force_sign(
        chain.session_key,
        child_pub=rogue_key.public_hex,
        scope=("link:publish", "org:rebind"),  # org:rebind not held by parent
        org=ORG,
        subject=Subject(kind="agent", id="rogue"),
        not_before=NOW,
        not_after=NOW + 60,
        parent_cert=chain.session_cert,
    )
    with pytest.raises(ScopeEscalationError):
        verify_chain(rogue, chain.root_pub, org=ORG, now=NOW)


def test_rejects_equal_scope_hop_strict_narrowing(chain):
    """Spec §3: each hop is STRICTLY narrower — equal scope is escalation."""
    rogue_key = KeyPair.generate()
    rogue = force_sign(
        chain.session_key,
        child_pub=rogue_key.public_hex,
        scope=chain.session_cert.scope,
        org=ORG,
        subject=Subject(kind="agent", id="rogue"),
        not_before=NOW,
        not_after=NOW + 60,
        parent_cert=chain.session_cert,
    )
    with pytest.raises(ScopeEscalationError):
        verify_chain(rogue, chain.root_pub, org=ORG, now=NOW)


def test_rejects_target_type_escalation(chain):
    parent_key = KeyPair.generate()
    parent = issue_cert(
        chain.session_key,
        parent_key.public_hex,
        scope=("link:publish", "link:revoke"),
        org=ORG,
        subject=Subject(kind="agent", id="restricted"),
        not_before=NOW,
        not_after=NOW + HOUR,
        target_types=("present",),
        parent_cert=chain.session_cert,
    )
    child_key = KeyPair.generate()
    for bad_types in (("design",), None):  # outside parent's set / unrestricted
        rogue = force_sign(
            parent_key,
            child_pub=child_key.public_hex,
            scope=("link:publish",),
            org=ORG,
            subject=Subject(kind="agent", id="rogue"),
            not_before=NOW,
            not_after=NOW + 60,
            target_types=bad_types,
            parent_cert=parent,
        )
        with pytest.raises(ScopeEscalationError):
            verify_chain(rogue, chain.root_pub, org=ORG, now=NOW)


# --- TTL narrowing ------------------------------------------------------------


def test_rejects_child_with_equal_not_after(chain):
    rogue_key = KeyPair.generate()
    rogue = force_sign(
        chain.session_key,
        child_pub=rogue_key.public_hex,
        scope=("link:publish",),
        org=ORG,
        subject=Subject(kind="agent", id="rogue"),
        not_before=NOW,
        not_after=SESSION_WINDOW[1],  # equal, not strictly earlier
        parent_cert=chain.session_cert,
    )
    with pytest.raises(TTLViolationError):
        verify_chain(rogue, chain.root_pub, org=ORG, now=NOW)


def test_rejects_child_starting_before_parent(chain):
    rogue_key = KeyPair.generate()
    rogue = force_sign(
        chain.session_key,
        child_pub=rogue_key.public_hex,
        scope=("link:publish",),
        org=ORG,
        subject=Subject(kind="agent", id="rogue"),
        not_before=SESSION_WINDOW[0] - 10,
        not_after=NOW + 60,
        parent_cert=chain.session_cert,
    )
    with pytest.raises(TTLViolationError):
        verify_chain(rogue, chain.root_pub, org=ORG, now=NOW)


# --- acceptance: revoked key anywhere in chain --------------------------------


def test_rejects_revoked_leaf_key(chain):
    with pytest.raises(RevokedError):
        verify_chain(
            chain.agent_cert,
            chain.root_pub,
            org=ORG,
            now=NOW,
            revocations={chain.agent_key.public_hex},
        )


def test_rejects_revoked_intermediate_key(chain):
    """Revoking the session key kills every chain routed through it."""
    with pytest.raises(RevokedError):
        verify_chain(
            chain.agent_cert,
            chain.root_pub,
            org=ORG,
            now=NOW,
            revocations={chain.session_key.public_hex},
        )


def test_unrelated_revocation_does_not_block(chain):
    verify_chain(
        chain.agent_cert,
        chain.root_pub,
        org=ORG,
        now=NOW,
        revocations={KeyPair.generate().public_hex},
    )


# --- acceptance: wrong-org cert ------------------------------------------------


def test_rejects_chain_minted_for_another_org(chain):
    foreign = build_chain(org=OTHER_ORG)
    # Same structure, wrong org for this verification context — and wrong
    # root; check against ITS OWN root so org is the only mismatch.
    with pytest.raises(WrongOrgError):
        verify_chain(foreign.agent_cert, foreign.root_pub, org=ORG, now=NOW)


def test_rejects_mixed_org_hop(chain):
    rogue_key = KeyPair.generate()
    rogue = force_sign(
        chain.session_key,
        child_pub=rogue_key.public_hex,
        scope=("link:publish",),
        org=OTHER_ORG,  # hop claims a different org than its ancestry
        subject=Subject(kind="agent", id="rogue"),
        not_before=NOW,
        not_after=NOW + 60,
        parent_cert=chain.session_cert,
    )
    with pytest.raises(WrongOrgError):
        verify_chain(rogue, chain.root_pub, org=ORG, now=NOW)


# --- acceptance: tampered signature --------------------------------------------


def _flip_last_hex_char(sig: str) -> str:
    return sig[:-1] + ("0" if sig[-1] != "0" else "1")


def test_rejects_tampered_leaf_signature(chain):
    tampered = dataclasses.replace(chain.agent_cert, sig=_flip_last_hex_char(chain.agent_cert.sig))
    with pytest.raises(SignatureError):
        verify_chain(tampered, chain.root_pub, org=ORG, now=NOW)


def test_rejects_tampered_payload_with_original_signature(chain):
    tampered = dataclasses.replace(chain.agent_cert, scope=("link:publish", "link:revoke"))
    with pytest.raises(SignatureError):
        verify_chain(tampered, chain.root_pub, org=ORG, now=NOW)


def test_rejects_tampered_intermediate_cert(chain):
    doctored_parent = dataclasses.replace(chain.session_cert, subject=Subject(kind="operator", id="evil"))
    tampered = dataclasses.replace(chain.agent_cert, parent_cert=doctored_parent)
    with pytest.raises(SignatureError):
        verify_chain(tampered, chain.root_pub, org=ORG, now=NOW)


def test_rejects_swapped_intermediate_cert(chain):
    """Splicing the agent cert onto a DIFFERENT (validly signed) parent must
    fail: the agent hop's signature covers its embedded ancestry."""
    other = build_chain()
    spliced = dataclasses.replace(chain.agent_cert, parent_cert=other.session_cert)
    with pytest.raises(SignatureError):
        verify_chain(spliced, chain.root_pub, org=ORG, now=NOW)


def test_rejects_wrong_root_key(chain):
    with pytest.raises(SignatureError):
        verify_chain(chain.agent_cert, KeyPair.generate().public_hex, org=ORG, now=NOW)


def test_rejects_signature_made_without_domain_separation(chain):
    """A signature over the bare canonical payload (no CERT_DOMAIN prefix)
    must not verify — cross-protocol replay is structurally impossible."""
    rogue_key = KeyPair.generate()
    unsigned = force_sign(  # start from a properly signed cert...
        chain.root,
        child_pub=rogue_key.public_hex,
        scope=("link:publish",),
        org=ORG,
        subject=Subject(kind="agent", id="rogue"),
        not_before=NOW,
        not_after=NOW + 60,
    )
    # ...then replace its sig with one omitting the domain prefix.
    bare_sig = chain.root.sign_hex(unsigned.payload_bytes())
    undomained = dataclasses.replace(unsigned, sig=bare_sig)
    with pytest.raises(SignatureError):
        verify_chain(undomained, chain.root_pub, org=ORG, now=NOW)


# --- structural hardening -------------------------------------------------------


def test_rejects_chain_deeper_than_max_depth(chain):
    cert = chain.session_cert
    for _ in range(MAX_CHAIN_DEPTH + 1):
        cert = dataclasses.replace(chain.session_cert, parent_cert=cert)
    with pytest.raises(MalformedError):
        verify_chain(cert, chain.root_pub, org=ORG, now=NOW)


def test_rejects_malformed_root_pub(chain):
    with pytest.raises(MalformedError):
        verify_chain(chain.agent_cert, "not-a-key", org=ORG, now=NOW)


def test_rejects_non_cert_input(chain):
    with pytest.raises(MalformedError):
        verify_chain({"v": 1}, chain.root_pub, org=ORG, now=NOW)


def test_issue_cert_refuses_to_mint_violations(chain):
    """Issuance-time defense in depth: a well-behaved issuer cannot mint
    what verification would reject."""
    key = KeyPair.generate()
    with pytest.raises(ScopeEscalationError):
        issue_cert(
            chain.session_key,
            key.public_hex,
            scope=("org:rebind",),
            org=ORG,
            subject=Subject(kind="agent", id="x"),
            not_before=NOW,
            not_after=NOW + 60,
            parent_cert=chain.session_cert,
        )
    with pytest.raises(TTLViolationError):
        issue_cert(
            chain.session_key,
            key.public_hex,
            scope=("link:publish",),
            org=ORG,
            subject=Subject(kind="agent", id="x"),
            not_before=NOW,
            not_after=SESSION_WINDOW[1] + 1,
            parent_cert=chain.session_cert,
        )
    with pytest.raises(MalformedError):
        issue_cert(
            chain.session_key,
            key.public_hex,
            scope=("link:publish",),
            org=OTHER_ORG,
            subject=Subject(kind="agent", id="x"),
            not_before=NOW,
            not_after=NOW + 60,
            parent_cert=chain.session_cert,
        )
    with pytest.raises(MalformedError):
        # signing key does not match the parent cert's child
        issue_cert(
            chain.agent_key,
            key.public_hex,
            scope=("link:publish",),
            org=ORG,
            subject=Subject(kind="agent", id="x"),
            not_before=NOW,
            not_after=NOW + 60,
            parent_cert=chain.session_cert,
        )
