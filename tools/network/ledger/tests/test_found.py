"""found_org_ledger: the four constitutional events and the folded result."""

from __future__ import annotations

import os

import pytest

from tools.network.idkit import KeyPair, derive_persona
from tools.network.idkit.sealing import open as seal_open
from tools.network.idkit.sealing import seal
from tools.network.ledger import (
    make_event, HLC, fold, sign_rotate_continuity, sign_rotate_recovery,
)
from tools.network.ledger.errors import SchemaError
from tools.network.ledger.found import FoundedLedger, found_org_ledger
from tools.network.ledger.fold import (
    R_INVITE_ALREADY_CLAIMED, R_INVITE_EXPIRED, R_RECOVERY_CONTINUITY_MISSING,
)
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


class TestResumeFounding:
    """Guarded completion onto an existing genesis (identity-preserving)."""

    def _fresh(self):
        return LedgerStore(), KeyPair.generate(), os.urandom(32)

    def _found_reference(self):
        """A complete founding to copy prefixes from."""
        store, root, seed = self._fresh()
        result = found_org_ledger(
            store, org_id=ORG_ID, org_root=root, personal_root_seed=seed, now=T0
        )
        return store, root, seed, result

    def _prefix_store(self, source: LedgerStore, event_ids) -> LedgerStore:
        replica = LedgerStore()
        for event_id in event_ids:
            replica.append(source.get(event_id))
        return replica

    def test_resume_from_genesis_only(self):
        from tools.network.ledger.found import resume_org_founding

        store, root, seed, original = self._found_reference()
        partial = self._prefix_store(store, [original.genesis_id])
        resumed = resume_org_founding(
            partial, org_id=ORG_ID, org_root=root, personal_root_seed=seed
        )
        assert len(partial) == 4
        # Deterministic completion: the resumed events ARE the original
        # events (same payloads, parents, HLC chain, deterministic sigs).
        assert resumed == original
        state = partial.fold()
        member = state.members[resumed.founder_persona_pub]
        assert member.roles == ("owner",)

    def test_resume_from_three_event_prefix(self):
        from tools.network.ledger.found import resume_org_founding

        store, root, seed, original = self._found_reference()
        partial = self._prefix_store(
            store,
            [original.genesis_id, original.role_define_id, original.founding_invite_id],
        )
        resumed = resume_org_founding(
            partial, org_id=ORG_ID, org_root=root, personal_root_seed=seed
        )
        assert resumed.founder_claim_id == original.founder_claim_id
        assert len(partial) == 4
        # The resumed claim folds VALID: the logical clock stayed at the
        # founding instant, so the spent-at-mint invite is not expired.
        state = partial.fold()
        assert state.valid[resumed.founder_claim_id] is True
        assert resumed.founder_persona_pub in state.members

    def test_resume_is_idempotent_on_a_complete_founding(self):
        from tools.network.ledger.found import resume_org_founding

        store, root, seed, original = self._found_reference()
        resumed = resume_org_founding(
            store, org_id=ORG_ID, org_root=root, personal_root_seed=seed
        )
        assert len(store) == 4
        assert resumed.founder_claim_id == original.founder_claim_id

    def test_resume_guards_fail_closed(self):
        from tools.network.ledger.found import (
            FoundingMismatchError,
            resume_org_founding,
        )

        store, root, seed, original = self._found_reference()
        partial = self._prefix_store(store, [original.genesis_id])
        with pytest.raises(FoundingMismatchError):  # foreign org root
            resume_org_founding(
                partial, org_id=ORG_ID, org_root=KeyPair.generate(),
                personal_root_seed=seed,
            )
        with pytest.raises(FoundingMismatchError):  # wrong org label
            resume_org_founding(
                partial, org_id="0" * 36, org_root=root, personal_root_seed=seed
            )
        assert len(partial) == 1  # nothing appended by refused completions

        # A different personal seed re-derives a different founder: the
        # committed invitation's key binding no longer matches.
        three = self._prefix_store(
            store,
            [original.genesis_id, original.role_define_id, original.founding_invite_id],
        )
        with pytest.raises(FoundingMismatchError):
            resume_org_founding(
                three, org_id=ORG_ID, org_root=root,
                personal_root_seed=os.urandom(32),
            )
        assert len(three) == 3

    def test_resume_refuses_a_non_prefix(self):
        from tools.network.ledger.found import (
            FoundingMismatchError,
            resume_org_founding,
        )

        store, root, seed = self._fresh()
        genesis_id = store.append(
            make_event(
                root,
                {"type": "genesis", "org": ORG_ID, "root_pub": root.public_hex},
                [],
                HLC(T0, 0),
            )
        )
        founder = KeyPair.generate()
        store.append(  # an invite with NO owner role definition beneath it
            make_event(
                root,
                {
                    "type": "invite",
                    "granted_role": "owner",
                    "expiry": T0,
                    "sponsor": root.public_hex,
                    "invite_pub": founder.public_hex,
                },
                [genesis_id],
                HLC(T0, 1),
            )
        )
        with pytest.raises(FoundingMismatchError):
            resume_org_founding(
                store, org_id=ORG_ID, org_root=root, personal_root_seed=os.urandom(32)
            )


def _rotate(store, org_root, new_root, *, genesis_id, recovery=None, ts):
    payload = {
        "type": "key.rotate",
        "old_pub": org_root.public_hex,
        "new_pub": new_root.public_hex,
        "continuity": sign_rotate_continuity(new_root, org_root.public_hex),
    }
    if recovery is not None:
        payload["recovery_continuity"] = sign_rotate_recovery(
            recovery, genesis_id, org_root.public_hex, new_root.public_hex
        )
    ev = make_event(org_root, payload, list(store.ledger.heads()), HLC(ts, 0))
    store.append(ev)
    return ev.event_id


def test_founding_with_recovery_declares_genesis_and_gates_rotation():
    # Enrolling a recovery factor at founding declares it constitutionally at
    # genesis, and the fold then requires the recovery co-signature to rotate --
    # a dual-signed rotation admits, a root-only one is refused.
    store = LedgerStore()
    org_root = KeyPair.generate()
    recovery = KeyPair.generate()
    result = found_org_ledger(
        store, org_id=ORG_ID, org_root=org_root,
        personal_root_seed=os.urandom(32), now=T0,
        recovery_pub=recovery.public_hex,
    )
    assert store.get(result.genesis_id).payload["recovery"] == {
        "policy": "recovery-key", "recovery_pub": recovery.public_hex,
    }
    dual = _rotate(store, org_root, KeyPair.generate(),
                   genesis_id=result.genesis_id, recovery=recovery, ts=T0 + 1000)
    assert fold(store.ledger).valid[dual] is True


def test_founding_with_recovery_refuses_root_only_rotation():
    # Under the founding-declared recovery-key policy a rotation missing the
    # recovery co-signature is refused -- a stolen root alone cannot rotate.
    store = LedgerStore()
    org_root = KeyPair.generate()
    recovery = KeyPair.generate()
    result = found_org_ledger(
        store, org_id=ORG_ID, org_root=org_root,
        personal_root_seed=os.urandom(32), now=T0,
        recovery_pub=recovery.public_hex,
    )
    root_only = _rotate(store, org_root, KeyPair.generate(),
                        genesis_id=result.genesis_id, ts=T0 + 1000)
    assert fold(store.ledger).reasons[root_only] == R_RECOVERY_CONTINUITY_MISSING


def test_founding_without_recovery_is_policy_none():
    store = LedgerStore()
    org_root = KeyPair.generate()
    result = found_org_ledger(
        store, org_id=ORG_ID, org_root=org_root,
        personal_root_seed=os.urandom(32), now=T0,
    )
    # No recovery field declared -> policy 'none' -> a plain rotation admits.
    assert "recovery" not in store.get(result.genesis_id).payload
    new_root = KeyPair.generate()
    plain = _rotate(store, org_root, new_root, genesis_id=result.genesis_id,
                    ts=T0 + 1000)
    assert fold(store.ledger).valid[plain] is True


def test_founding_rejects_recovery_pub_equals_root_pub():
    # The recovery factor must be a key the root does not control; naming the
    # root as its own recovery factor is the self-defeat rejected at genesis.
    store = LedgerStore()
    org_root = KeyPair.generate()
    with pytest.raises(SchemaError):
        found_org_ledger(
            store, org_id=ORG_ID, org_root=org_root,
            personal_root_seed=os.urandom(32), now=T0,
            recovery_pub=org_root.public_hex,
        )
