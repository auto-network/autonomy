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
    wrong_context = sign_delegate_proof(child, genesis, issuer, scope)
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
        second, agent, SCOPE, parents=frontier,
        proof=sign_delegate_proof(
            agent, sim.genesis_id, second.public_hex, SCOPE,
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
        )

    grant = delegate_mod.authorize_member_storage(
        sim.ledger, sim.root, persona, domain,
        hlc=HLC(sim.next_ts()),
        child_proof=sign_delegate_proof(
            persona, sim.genesis_id, sim.root.public_hex,
            storage_delegate_scopes(domain),
        ),
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
        ),
    )
    minted = delegate_mod.provision(
        sim.ledger, issuer, issuer, sim.genesis_id, hlc=HLC(sim.next_ts()),
    )
    state = sim.fold()
    assert state.valid[minted.grant_event_id] is True
    assert state.delegation_parents[minted.child_pub] == (issuer.public_hex,)
