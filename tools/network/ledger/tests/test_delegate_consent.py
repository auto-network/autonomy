"""A delegation grant proves the delegate consented (auto-le0kg).

Design of record graph://21a0da9e-1c2, driver D9. A ``delegate`` event
carries the named child key's signature over the grant itself — genesis,
issuer and scopes bound — so a member with delegation authority cannot
publish a grant over a public key they merely observed and take
attribution for every row the honest holder signs.
"""

from __future__ import annotations

import pytest

from tools.network.idkit import KeyPair
from tools.network.ledger import HLC, Ledger, fold, make_event, sign_delegate_proof
from tools.network.ledger.errors import SchemaError
from tools.network.ledger.fold import R_DELEGATE_UNPROVEN

from .conftest import Sim

SCOPE = ["link:publish"]
BOTH = ["link:publish", "link:revoke"]


def test_a_delegate_event_without_a_proof_is_refused_at_validation():
    sim = Sim()
    with pytest.raises(SchemaError, match="proof"):
        make_event(
            sim.root,
            {
                "type": "delegate",
                "child_pub": KeyPair.generate().public_hex,
                "scope": SCOPE,
                "can_redelegate": False,
            },
            sim.ledger.heads(),
            HLC(1_800_000_001_000),
        )


@pytest.mark.parametrize(
    "twist",
    ["different-genesis", "different-issuer", "different-scope"],
)
def test_a_proof_from_any_other_context_is_refused(twist):
    """The binding covers genesis, issuer and scopes; a signature obtained
    elsewhere — another organization, another grant — does not verify."""
    sim = Sim()
    child = KeyPair.generate()
    genesis = "9f" * 32 if twist == "different-genesis" else sim.genesis_id
    issuer = (
        KeyPair.generate().public_hex if twist == "different-issuer"
        else sim.root.public_hex
    )
    scope = ["link:revoke"] if twist == "different-scope" else SCOPE
    wrong_context = sign_delegate_proof(
        child, genesis, issuer, scope, grant_nonce="ab" * 32,
    )
    grant = sim.delegate(sim.root, child, SCOPE, proof=wrong_context)
    state = sim.fold()
    assert state.valid[grant] is False
    assert state.reasons[grant] == R_DELEGATE_UNPROVEN
    assert child.public_hex not in state.delegation_parents


def two_issuers():
    """A sim with two members each holding BOTH scopes re-delegably, and an
    agent key whose honest grant comes from the first."""
    sim = Sim()
    honest, attacker = KeyPair.generate(), KeyPair.generate()
    sim.delegate(sim.root, honest, BOTH, redelegate=True)
    sim.delegate(sim.root, attacker, BOTH, redelegate=True)
    agent = KeyPair.generate()
    sim.delegate(honest, agent, SCOPE)
    return sim, honest, attacker, agent


def test_a_grant_over_a_merely_observed_key_is_refused():
    """The attack the bead names: the agent's public key appears in the
    honest grant and in every row it signs, so the attacker holds it — and
    holds no part of the private key, so their grant dies unproven and
    attribution continues to report only the honest author."""
    sim, honest, attacker, agent = two_issuers()
    forged = sim.delegate(
        attacker, agent, SCOPE,
        proof=attacker.sign_hex(b"i-observed-this-key"),
    )
    state = sim.fold()
    assert state.valid[forged] is False
    assert state.reasons[forged] == R_DELEGATE_UNPROVEN
    assert state.delegation_parents[agent.public_hex] == (honest.public_hex,)


def test_two_consenting_grants_are_both_valid_and_attribution_is_deterministic():
    """Two members who each OBTAIN the delegate's signature both produce
    valid grants — proof of possession is what makes concurrent grants
    equally legitimate rather than one of them forged — and the existing
    sorted-first attribution answers identically in every delivery order."""
    sim, honest, second, agent = two_issuers()
    frontier = sim.ledger.heads()
    consented = sim.delegate(
        second, agent, SCOPE, parents=frontier, nonce="cd" * 32,
        proof=sign_delegate_proof(
            agent, sim.genesis_id, second.public_hex, SCOPE,
            grant_nonce="cd" * 32,
        ),
    )
    state = sim.fold()
    assert state.valid[consented] is True
    assert state.delegation_parents[agent.public_hex] == tuple(
        sorted((honest.public_hex, second.public_hex))
    )

    # Same statements delivered to a second store in a different (causal)
    # order: the fold is a pure function of the event set, so attribution
    # cannot differ between stores.
    replay = Ledger()
    delivered: set = set()
    pending = list(sim.ledger.events())
    while pending:
        # Among deliverable events, take the CONTESTED grant first when
        # ready, else the one the first store happened to add last.
        ready = [e for e in pending if all(p in delivered for p in e.parents)]
        assert ready, "causal delivery stalled"
        ready.sort(key=lambda e: (e.event_id != consented, e.event_id), reverse=False)
        candidate = ready[0]
        replay.add(candidate)
        delivered.add(candidate.event_id)
        pending.remove(candidate)
    assert fold(replay).delegation_parents[agent.public_hex] == tuple(
        sorted((honest.public_hex, second.public_hex))
    )


def test_authorize_member_storage_requires_the_members_proof():
    from tools.network.ledger.projections import organization_content_domain_id
    from tools.network.storagekit import delegate as delegate_mod
    from tools.network.storagekit import storage_delegate_scopes

    sim = Sim()
    persona = KeyPair.generate()
    sim.role_define(sim.root, "member", requires="self")
    invite = sim.invite(sim.root, "member", invite_key=persona)
    sim.claim(invite, persona, persona)
    domain = organization_content_domain_id(sim.genesis_id)

    with pytest.raises(delegate_mod.DelegateError, match="consent"):
        delegate_mod.authorize_member_storage(
            sim.ledger, sim.root, persona, domain,
            hlc=HLC(sim.next_ts()), child_proof="",
            grant_nonce="55" * 32,
        )

    grant = delegate_mod.authorize_member_storage(
        sim.ledger, sim.root, persona, domain,
        hlc=HLC(sim.next_ts()),
        child_proof=sign_delegate_proof(
            persona, sim.genesis_id, sim.root.public_hex,
            storage_delegate_scopes(domain),
            can_redelegate=True, grant_nonce="51" * 32,
        ),
        grant_nonce="51" * 32,
    )
    assert sim.fold().valid[grant] is True


def test_provision_produces_its_own_proof_and_needs_no_extra_input():
    """provision holds the child private half it just generated, so consent
    costs nothing — the minted grant folds valid with no new argument."""
    from tools.network.ledger.projections import organization_content_domain_id
    from tools.network.storagekit import delegate as delegate_mod
    from tools.network.storagekit import storage_delegate_scopes

    sim = Sim()
    issuer = KeyPair.generate()
    sim.role_define(sim.root, "member", requires="self")
    invite = sim.invite(sim.root, "member", invite_key=issuer)
    sim.claim(invite, issuer, issuer)
    domain = organization_content_domain_id(sim.genesis_id)
    delegate_mod.authorize_member_storage(
        sim.ledger, sim.root, issuer, domain,
        hlc=HLC(sim.next_ts()),
        child_proof=sign_delegate_proof(
            issuer, sim.genesis_id, sim.root.public_hex,
            storage_delegate_scopes(domain),
            can_redelegate=True, grant_nonce="52" * 32,
        ),
        grant_nonce="52" * 32,
    )
    minted = delegate_mod.provision(
        sim.ledger, issuer, issuer, sim.genesis_id, hlc=HLC(sim.next_ts()),
    )
    state = sim.fold()
    assert state.valid[minted.grant_event_id] is True
    assert state.delegation_parents[minted.child_pub] == (issuer.public_hex,)


# ── v2: the proof covers the full terms and a single-use nonce ──────────
#
# Operator-approved hard cut (2026-08-19): domain delegate-consent.v2, no
# v1 acceptance path. The terms binding closes WIDENING (a grant the child
# consented to cannot be reissued with wider authority under its own
# signature); the nonce is what makes consent REVOCABLE rather than
# perpetual — the replay below was ADMITTED under v1, demonstrated on the
# pre-fix commit: revoke carried in the republication's ancestry, kill
# check never reached it, child re-delegated on withdrawn consent.


def test_a_replayed_proof_after_revoke_is_refused():
    """THE attack this revision exists to stop: grant, revoke, republish
    byte-identical terms with the original proof. The republication is
    refused on nonce reuse — not on the proof, which still verifies."""
    from tools.network.ledger.fold import R_DELEGATE_NONCE_REUSED

    sim = Sim()
    child = KeyPair.generate()
    nonce = "ee" * 32
    proof = sign_delegate_proof(
        child, sim.genesis_id, sim.root.public_hex, SCOPE, grant_nonce=nonce,
    )
    g1 = sim.delegate(sim.root, child, SCOPE, proof=proof, nonce=nonce)
    sim.revoke_event(sim.root, g1)
    replayed = sim.delegate(sim.root, child, SCOPE, proof=proof, nonce=nonce)
    state = sim.fold()
    assert state.valid[g1] is True
    assert state.valid[replayed] is False
    assert state.reasons[replayed] == R_DELEGATE_NONCE_REUSED
    assert state.delegation_parents.get(child.public_hex) is None, (
        "withdrawn consent must not re-delegate the child"
    )


def test_a_fresh_consent_after_revoke_is_admitted():
    """The designed escape: the child signs AGAIN, over a fresh nonce.
    Revocation withdraws a consent; it does not ban the relationship."""
    sim = Sim()
    child = KeyPair.generate()
    g1 = sim.delegate(sim.root, child, SCOPE)
    sim.revoke_event(sim.root, g1)
    regrant = sim.delegate(sim.root, child, SCOPE)  # helper mints new nonce
    state = sim.fold()
    assert state.valid[regrant] is True
    assert state.delegation_parents[child.public_hex] == (sim.root.public_hex,)


@pytest.mark.parametrize("widening", ["can_redelegate", "ttl"])
def test_widened_terms_break_the_proof(widening):
    """The second gap: a proof that omitted the attenuation terms let a
    consented grant be reissued with WIDER authority. Now the issuer
    altering can_redelegate or ttl after the child signed dies unproven."""
    sim = Sim()
    child = KeyPair.generate()
    nonce = "cc" * 32
    proof = sign_delegate_proof(
        child, sim.genesis_id, sim.root.public_hex, SCOPE,
        can_redelegate=False, ttl=60_000, grant_nonce=nonce,
    )
    if widening == "can_redelegate":
        grant = sim.delegate(
            sim.root, child, SCOPE, redelegate=True, ttl=60_000,
            proof=proof, nonce=nonce,
        )
    else:
        grant = sim.delegate(
            sim.root, child, SCOPE, ttl=3_600_000, proof=proof, nonce=nonce,
        )
    state = sim.fold()
    assert state.valid[grant] is False
    assert state.reasons[grant] == R_DELEGATE_UNPROVEN


def test_scope_order_does_not_change_the_proof():
    """The sort is part of the binding: the child may sign the scopes in
    any order and the grant verifies — the same grant can never produce
    two different signatures."""
    sim = Sim()
    child = KeyPair.generate()
    nonce = "dd" * 32
    proof = sign_delegate_proof(
        child, sim.genesis_id, sim.root.public_hex,
        ["link:revoke", "link:publish", "link:publish"], grant_nonce=nonce,
    )
    grant = sim.delegate(sim.root, child, BOTH, proof=proof, nonce=nonce)
    assert sim.fold().valid[grant] is True


def test_a_refused_grant_does_not_burn_its_nonce():
    """Only an ADMITTED grant consumes a nonce: a grant refused for scope
    escalation may be corrected and re-issued under the same consent
    ceremony's nonce discipline without the refusal blocking it."""
    sim = Sim()
    member = KeyPair.generate()
    sim.delegate(sim.root, member, SCOPE, redelegate=True)
    child = KeyPair.generate()
    nonce = "ff" * 32
    # Member over-reaches: BOTH scopes when it re-delegably holds one.
    over = sim.delegate(
        member, child, BOTH, nonce=nonce,
        proof=sign_delegate_proof(
            child, sim.genesis_id, member.public_hex, BOTH, grant_nonce=nonce,
        ),
    )
    corrected = sim.delegate(
        member, child, SCOPE, nonce=nonce,
        proof=sign_delegate_proof(
            child, sim.genesis_id, member.public_hex, SCOPE, grant_nonce=nonce,
        ),
    )
    state = sim.fold()
    assert state.reasons[over] == "scope-escalation"
    assert state.valid[corrected] is True


def test_a_malformed_nonce_is_refused_at_validation():
    sim = Sim()
    child = KeyPair.generate()
    for bad in ("", "zz" * 32, "ab" * 16):
        with pytest.raises(SchemaError, match="grant_nonce"):
            make_event(
                sim.root,
                {
                    "type": "delegate",
                    "child_pub": child.public_hex,
                    "scope": SCOPE,
                    "can_redelegate": False,
                    "grant_nonce": bad,
                    "proof": "ab" * 64,
                },
                sim.ledger.heads(),
                HLC(1_800_000_001_000),
            )


def test_renewal_signs_anew_and_the_two_grants_carry_distinct_nonces():
    """Amendment 3 made observable: renew() reuses the child key but signs
    a FRESH consent — the renewal folds valid and the two grant events
    carry different nonces, so neither is a replay of the other."""
    from tools.network.ledger.projections import organization_content_domain_id
    from tools.network.storagekit import delegate as delegate_mod
    from tools.network.storagekit import storage_delegate_scopes

    sim = Sim()
    issuer = KeyPair.generate()
    sim.role_define(sim.root, "member", requires="self")
    invite = sim.invite(sim.root, "member", invite_key=issuer)
    sim.claim(invite, issuer, issuer)
    domain = organization_content_domain_id(sim.genesis_id)
    delegate_mod.authorize_member_storage(
        sim.ledger, sim.root, issuer, domain, hlc=HLC(sim.next_ts()),
        child_proof=sign_delegate_proof(
            issuer, sim.genesis_id, sim.root.public_hex,
            storage_delegate_scopes(domain),
            can_redelegate=True, grant_nonce="56" * 32,
        ),
        grant_nonce="56" * 32,
    )
    minted = delegate_mod.provision(
        sim.ledger, issuer, issuer, sim.genesis_id, hlc=HLC(sim.next_ts()),
    )
    renewed = delegate_mod.renew(
        sim.ledger, issuer, minted, hlc=HLC(sim.next_ts()),
    )
    state = sim.fold()
    assert state.valid[minted.grant_event_id] is True
    assert state.valid[renewed.grant_event_id] is True
    assert renewed.child_pub == minted.child_pub
    n1 = sim.ledger.get(minted.grant_event_id).payload["grant_nonce"]
    n2 = sim.ledger.get(renewed.grant_event_id).payload["grant_nonce"]
    assert n1 != n2
