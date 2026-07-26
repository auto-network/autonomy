"""Claim, pending, and approval flow — the fold/store/mint half (auto-g6q9d)."""

from __future__ import annotations

import hashlib
import os

import pytest

from tools.network.idkit import KeyPair, derive_persona, generate_token
from tools.network.ledger import HLC, LedgerStore, make_event, sign_approval
from tools.network.ledger.claims import build_member_claim_payload, mint_member_claim
from tools.network.ledger.errors import SchemaError, SignatureError
from tools.network.ledger.fold import (
    R_APPROVAL_MISSING,
    R_CLAIM_BAD_CREDENTIAL,
    R_CLAIM_BAD_TOKEN,
)
from tools.network.ledger.store import StoreError

ORG_ID = "018f6b2a-7c4d-7e11-8a3b-9d5c1e2f4a6b"
T0 = 1_800_000_000_000


class Org:
    """A LedgerStore-backed org with a role and one invite, plus helpers."""

    def __init__(self, path=":memory:", requires="self", threshold=None, token=True):
        self.store = LedgerStore(path)
        self.root = KeyPair.generate()
        self._ts = T0
        self.genesis_id = self._emit(
            self.root,
            {"type": "genesis", "org": ORG_ID, "root_pub": self.root.public_hex},
            parents=[],
        )
        define = {
            "type": "role.define",
            "name": "member",
            "scope_set": ["link:publish"],
            "claim_requires": requires,
            "version": 1,
        }
        if threshold is not None:
            define["approver_threshold"] = {"kind": "static", "count": threshold}
        self._emit(self.root, define)
        # A delegated admin who can approve admissions into 'member'.
        self.admin = KeyPair.generate()
        self._emit(
            self.root,
            {
                "type": "delegate",
                "child_pub": self.admin.public_hex,
                "scope": ["role:grant:member"],
                "can_redelegate": False,
            },
        )
        self.seed = os.urandom(32)
        self.persona = derive_persona(self.seed, self.genesis_id)
        if token:
            self.token = generate_token()
            invite_payload = {
                "type": "invite",
                "granted_role": "member",
                "expiry": T0 + 10**9,
                "sponsor": self.root.public_hex,
                "token_hash": hashlib.sha256(self.token.encode()).hexdigest(),
            }
        else:
            self.token = None
            invite_payload = {
                "type": "invite",
                "granted_role": "member",
                "expiry": T0 + 10**9,
                "sponsor": self.root.public_hex,
                "invite_pub": self.persona.public_hex,
            }
        self.invite_id = self._emit(self.root, invite_payload)

    def _emit(self, author, payload, parents=None):
        self._ts += 1_000
        return self.store.append(
            make_event(
                author,
                payload,
                sorted(self.store.heads()) if parents is None else parents,
                HLC(self._ts),
            )
        )

    def next_hlc(self) -> HLC:
        self._ts += 1_000
        return HLC(self._ts)

    def mint_claim(self, approvers=(), token=None, **kw):
        payload = build_member_claim_payload(
            self.persona.public_hex,
            self.invite_id,
            token=self.token if token is None else token,
            **kw,
        )
        entries = [sign_approval(a, "member.claim", payload) for a in approvers]
        event, persona = mint_member_claim(
            self.seed,
            self.genesis_id,
            invite_ref=self.invite_id,
            heads=self.store.heads(),
            hlc=self.next_hlc(),
            token=self.token if token is None else token,
            approvals=entries,
            **kw,
        )
        assert persona.public_hex == self.persona.public_hex
        return event


# -- bearer safety: the invariant's teeth --------------------------------------------


def test_token_claim_never_admits_via_direct_append():
    org = Org(requires="self", token=True)  # 'self' is the permissive case
    bare = org.mint_claim()
    # The headless-client bypass: straight through LedgerStore.append,
    # no API in the path. Persisted (structurally valid) but INERT.
    org.store.append(bare)
    state = org.store.fold()
    assert state.valid[bare.event_id] is False
    assert state.reasons[bare.event_id] == R_APPROVAL_MISSING
    assert org.persona.public_hex not in state.members


@pytest.mark.parametrize("approver", ["sponsor-root", "admin"])
def test_one_countersignature_admits_a_token_claim(approver):
    org = Org(requires="self", token=True)
    signer = org.root if approver == "sponsor-root" else org.admin
    signed = org.mint_claim(approvers=[signer])
    org.store.append(signed)
    state = org.store.fold()
    assert state.valid[signed.event_id] is True
    assert state.members[org.persona.public_hex].roles == ("member",)


def test_key_bound_self_claim_admits_immediately():
    org = Org(requires="self", token=False)
    claim = org.mint_claim()
    org.store.append(claim)
    state = org.store.fold()
    assert state.valid[claim.event_id] is True
    assert org.persona.public_hex in state.members


# -- evaluate / stage / countersign / admit -------------------------------------------


def test_full_pending_flow_admin_ack_threshold_two():
    org = Org(requires="admin-ack", threshold=2, token=True)
    admin2 = KeyPair.generate()
    org._emit(
        org.root,
        {
            "type": "delegate",
            "child_pub": admin2.public_hex,
            "scope": ["role:grant:member"],
            "can_redelegate": False,
        },
    )
    bare = org.mint_claim()
    events_before = len(org.store)

    # Submit-time triage: under-approved -> stage, nothing persisted.
    assert org.store.evaluate_claim(bare) == R_APPROVAL_MISSING
    assert len(org.store) == events_before  # trial fold persisted nothing
    claim_key = org.store.stage_pending_claim(bare)
    assert org.store.get_pending_claim(claim_key)["approvals"] == []

    # First authorized countersignature: merged, still under threshold.
    body = org.store.get_pending_claim(claim_key)["body"]
    entry1 = sign_approval(org.admin, "member.claim", body)
    merged = org.store.add_pending_approval(claim_key, entry1)
    assert [e["key"] for e in merged] == sorted([org.admin.public_hex])
    still = org.mint_claim(approvers=[org.admin])
    assert org.store.evaluate_claim(still) == R_APPROVAL_MISSING

    # Second one satisfies the threshold; finalize by re-minting with the
    # merged approvals under the invitee's key and appending for real.
    entry2 = sign_approval(admin2, "member.claim", body)
    merged = org.store.add_pending_approval(claim_key, entry2)
    final, _ = mint_member_claim(
        org.seed,
        org.genesis_id,
        invite_ref=org.invite_id,
        heads=org.store.heads(),
        hlc=org.next_hlc(),
        token=org.token,
        approvals=merged,
    )
    assert org.store.evaluate_claim(final) is None
    org.store.append(final)
    state = org.store.fold()
    assert state.members[org.persona.public_hex].roles == ("member",)
    org.store.drop_pending_claim(claim_key)
    assert org.store.get_pending_claim(claim_key) is None


def test_countersignature_verification_fails_closed():
    org = Org(requires="admin-ack", token=True)
    claim_key = org.store.stage_pending_claim(org.mint_claim())
    body = org.store.get_pending_claim(claim_key)["body"]

    forged = dict(sign_approval(org.admin, "member.claim", body))
    forged["sig"] = KeyPair.generate().sign_hex(b"unrelated")
    with pytest.raises(SignatureError):
        org.store.add_pending_approval(claim_key, forged)
    with pytest.raises(SchemaError):
        org.store.add_pending_approval(claim_key, {"key": org.admin.public_hex})
    with pytest.raises(StoreError):
        org.store.add_pending_approval("00" * 32, sign_approval(org.admin, "member.claim", body))
    assert org.store.get_pending_claim(claim_key)["approvals"] == []

    # An UNAUTHORIZED but well-signed countersignature merges at the store
    # (signature-valid) yet never admits: authority is the fold's call.
    rando = KeyPair.generate()
    merged = org.store.add_pending_approval(
        claim_key, sign_approval(rando, "member.claim", body)
    )
    final, _ = mint_member_claim(
        org.seed, org.genesis_id, invite_ref=org.invite_id,
        heads=org.store.heads(), hlc=org.next_hlc(),
        token=org.token, approvals=merged,
    )
    assert org.store.evaluate_claim(final) == R_APPROVAL_MISSING


def test_wrong_token_is_a_hard_rejection_not_a_staging():
    org = Org(requires="self", token=True)
    wrong = org.mint_claim(token="ab" * 32)
    assert org.store.evaluate_claim(wrong) == R_CLAIM_BAD_TOKEN  # never staged


def test_duplicate_approver_collapses_in_the_merge():
    org = Org(requires="admin-ack", token=True)
    claim_key = org.store.stage_pending_claim(org.mint_claim())
    body = org.store.get_pending_claim(claim_key)["body"]
    entry = sign_approval(org.admin, "member.claim", body)
    org.store.add_pending_approval(claim_key, entry)
    merged = org.store.add_pending_approval(claim_key, entry)
    assert len(merged) == 1


def test_pending_claims_survive_reopen(tmp_path):
    db = tmp_path / "org.db"
    org = Org(path=db, requires="admin-ack", token=True)
    claim_key = org.store.stage_pending_claim(org.mint_claim())
    body = org.store.get_pending_claim(claim_key)["body"]
    org.store.add_pending_approval(
        claim_key, sign_approval(org.admin, "member.claim", body)
    )
    org.store.close()

    reopened = LedgerStore(db)
    record = reopened.get_pending_claim(claim_key)
    assert record is not None
    assert [e["key"] for e in record["approvals"]] == [org.admin.public_hex]
    reopened.close()


# -- credential integration (jkd6f consumption) ----------------------------------------


def _credential_for(org):
    from tools.network.storagekit.credentials import build

    credential, _ = build(
        org.persona, org.genesis_id, os.urandom(32), [org.genesis_id], (T0, 0)
    )
    return credential.to_dict()


def test_claim_carries_the_kem_credential_through_the_flow():
    org = Org(requires="self", token=True)
    credential = _credential_for(org)
    signed = org.mint_claim(approvers=[org.root], kem_credential=credential)
    assert org.store.evaluate_claim(signed) is None
    org.store.append(signed)
    member = org.store.fold().members[org.persona.public_hex]
    assert member.kem_credential == credential


def test_tampered_credential_is_a_hard_rejection():
    org = Org(requires="self", token=True)
    credential = _credential_for(org)
    credential["signature"] = KeyPair.generate().sign_hex(b"unrelated")
    bad = org.mint_claim(approvers=[org.root], kem_credential=credential)
    assert org.store.evaluate_claim(bad) == R_CLAIM_BAD_CREDENTIAL  # never staged
