"""found_org_ledger: the four constitutional events and the folded result."""

from __future__ import annotations

import os

import pytest

from tools.network.idkit import KeyPair, derive_persona
from tools.network.idkit.sealing import open as seal_open
from tools.network.idkit.sealing import seal
from tools.network.ledger import make_event, HLC
from tools.network.ledger.found import FoundedLedger, found_org_ledger
from tools.network.ledger.fold import R_INVITE_ALREADY_CLAIMED, R_INVITE_EXPIRED
from tools.network.ledger.store import LedgerStore

ORG_ID = "018f6b2a-7c4d-7e11-8a3b-9d5c1e2f4a6b"  # uuid7-style orgs.id
T0 = 1_800_000_000_000


@pytest.fixture
def founding():
    store = LedgerStore()
    org_root = KeyPair.generate()
    seed = os.urandom(32)
    result = found_org_ledger(
        store, org_id=ORG_ID, org_root=org_root, personal_root_seed=seed, now=T0
    )
    return store, org_root, seed, result


def test_exactly_four_events_field_by_field(founding):
    store, org_root, seed, result = founding
    assert len(store) == 4
    assert result.genesis_id == store.ledger.genesis.event_id

    genesis = store.get(result.genesis_id)
    assert genesis.payload == {
        "type": "genesis",
        "org": ORG_ID,
        "root_pub": org_root.public_hex,
    }

    founder = derive_persona(seed, result.genesis_id)
    assert result.founder_persona_pub == founder.public_hex

    define = store.get(result.role_define_id)
    assert define.author_key == org_root.public_hex
    assert define.payload == {
        "type": "role.define",
        "name": "owner",
        "scope_set": ["*"],
        "claim_requires": "self",
        "version": 1,
    }

    invite = store.get(result.founding_invite_id)
    assert invite.author_key == org_root.public_hex
    assert invite.payload == {
        "type": "invite",
        "granted_role": "owner",
        "expiry": T0,
        "sponsor": org_root.public_hex,
        "invite_pub": founder.public_hex,
    }

    claim = store.get(result.founder_claim_id)
    assert claim.author_key == founder.public_hex
    assert claim.payload == {
        "type": "member.claim",
        "invite_ref": result.founding_invite_id,
        "persona_pub": founder.public_hex,
        "profile": {},
        "approvals": [],
    }
    # Default founding: no credential anywhere.
    assert "kem_credential" not in claim.payload
    assert result.kem_credential is None
    assert result.kem_private_key is None


def test_folded_founder_is_a_claimed_owner(founding):
    store, org_root, _, result = founding
    state = store.fold()
    member = state.members[result.founder_persona_pub]
    assert member.current_key == result.founder_persona_pub
    assert member.roles == ("owner",)
    assert member.sponsor == org_root.public_hex
    assert member.claim_id == result.founder_claim_id
    assert member.invite_id == result.founding_invite_id
    assert member.kem_credential is None
    assert state.authority(result.founder_persona_pub) == frozenset({"*"})
    assert state.holds(result.founder_persona_pub, "link:publish") is True
    assert state.bare_roles == {}


def test_kem_seed_path_carries_a_working_credential():
    store = LedgerStore()
    org_root = KeyPair.generate()
    seed, kem_seed = os.urandom(32), os.urandom(32)
    result = found_org_ledger(
        store,
        org_id=ORG_ID,
        org_root=org_root,
        personal_root_seed=seed,
        now=T0,
        kem_seed=kem_seed,
    )
    from tools.network.storagekit.credentials import validate

    credential = validate(result.kem_credential)
    assert credential.persona == result.founder_persona_pub
    assert credential.genesis_id == result.genesis_id

    # The claim carries it, the fold verified and projects it.
    claim = store.get(result.founder_claim_id)
    assert claim.payload["kem_credential"] == result.kem_credential
    state = store.fold()
    assert state.members[result.founder_persona_pub].kem_credential == result.kem_credential

    # The returned private key decapsulates a record sealed to the
    # credential's public key.
    record = seal(b"root armor material", credential.kem_public_key, "org-armor.v1")
    assert seal_open(record, result.kem_private_key, "org-armor.v1") == (
        b"root armor material"
    )


def test_founding_invite_admits_no_second_member(founding):
    store, _, seed, result = founding
    founder = derive_persona(seed, result.genesis_id)
    replay = make_event(
        founder,
        {
            "type": "member.claim",
            "invite_ref": result.founding_invite_id,
            "persona_pub": KeyPair.generate().public_hex,
            "profile": {},
            "approvals": [],
        },
        [result.founder_claim_id],
        HLC(T0 + 1_000, 0),
    )
    replay_id = store.append(replay)
    state = store.fold()
    assert len(store) == 5
    assert state.valid[replay_id] is False
    assert state.reasons[replay_id] in (R_INVITE_EXPIRED, R_INVITE_ALREADY_CLAIMED)
    assert len(state.members) == 1  # the founder alone


def test_genesis_must_be_self_signed():
    from tools.network.ledger.errors import GenesisError, SignatureError

    store = LedgerStore()
    root, imposter = KeyPair.generate(), KeyPair.generate()
    forged = make_event(
        imposter,
        {"type": "genesis", "org": ORG_ID, "root_pub": root.public_hex},
        [],
        HLC(T0, 0),
    )
    with pytest.raises((GenesisError, SignatureError)):
        store.append(forged)
