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
    R_INVITE_EXPIRED,
)
from tools.network.ledger.store import PENDING_CLAIM_TTL_MS, StoreError
from tools.network.ledger.events import sign_delegate_proof

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
                "proof": sign_delegate_proof(
                    self.admin, self.genesis_id, self.root.public_hex,
                    ["role:grant:member"],
                ),
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
            "proof": sign_delegate_proof(
                admin2, org.genesis_id, org.root.public_hex,
                ["role:grant:member"],
            ),
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


# -- the readiness seam (pre-merge addition; consumer: the sibling's routes) -----------


def test_readiness_tracks_the_fold_verdict():
    org = Org(requires="admin-ack", threshold=2, token=True)
    admin2 = KeyPair.generate()
    org._emit(
        org.root,
        {
            "type": "delegate",
            "child_pub": admin2.public_hex,
            "scope": ["role:grant:member"],
            "can_redelegate": False,
            "proof": sign_delegate_proof(
                admin2, org.genesis_id, org.root.public_hex,
                ["role:grant:member"],
            ),
        },
    )
    bare = org.mint_claim()
    claim_key = org.store.stage_pending_claim(bare)
    assert claim_key == org.store.claim_key(org.invite_id, org.persona.public_hex)

    status = org.store.evaluate_pending_claim(claim_key)
    assert (status["ready"], status["have"], status["need"]) == (False, 0, 2)
    assert (status["reason"], status["admitting"]) == (R_APPROVAL_MISSING, [])

    body = org.store.get_pending_claim(claim_key)["body"]
    org.store.add_pending_approval(claim_key, sign_approval(org.admin, "member.claim", body))
    status = org.store.evaluate_pending_claim(claim_key)
    assert (status["ready"], status["have"], status["need"]) == (False, 1, 2)
    assert status["admitting"] == [org.admin.public_hex]

    # Unauthorized-but-well-signed merges at the store yet moves nothing —
    # the store/fold split surfaced through the readiness seam.
    org.store.add_pending_approval(
        claim_key, sign_approval(KeyPair.generate(), "member.claim", body)
    )
    status = org.store.evaluate_pending_claim(claim_key)
    assert (status["ready"], status["have"]) == (False, 1)

    merged = org.store.add_pending_approval(
        claim_key, sign_approval(admin2, "member.claim", body)
    )
    status = org.store.evaluate_pending_claim(claim_key)
    assert (status["ready"], status["have"], status["need"]) == (True, 2, 2)
    assert status["reason"] is None
    assert status["admitting"] == sorted([org.admin.public_hex, admin2.public_hex])
    # The fixed causal position finalization must re-mint at (auto-cz4fb).
    assert status["position"]["parents"] == sorted(bare.parents)
    assert status["position"]["hlc"] == [bare.hlc.ts, bare.hlc.count]

    # The readiness verdict matches the fold's: finalize and admit.
    final, _ = mint_member_claim(
        org.seed, org.genesis_id, invite_ref=org.invite_id,
        heads=org.store.heads(), hlc=org.next_hlc(),
        token=org.token, approvals=merged,
    )
    assert org.store.evaluate_claim(final) is None
    org.store.append(final)
    assert org.persona.public_hex in org.store.fold().members


def test_readiness_sponsor_and_token_self_shapes():
    sponsor_org = Org(requires="sponsor", token=True)
    key = sponsor_org.store.stage_pending_claim(sponsor_org.mint_claim())
    assert sponsor_org.store.evaluate_pending_claim(key)["need"] == 1
    body = sponsor_org.store.get_pending_claim(key)["body"]
    sponsor_org.store.add_pending_approval(
        key, sign_approval(sponsor_org.root, "member.claim", body)
    )
    status = sponsor_org.store.evaluate_pending_claim(key)
    assert status["ready"] is True
    assert status["admitting"] == [sponsor_org.root.public_hex]

    token_self = Org(requires="self", token=True)  # bearer safety: need 1
    key2 = token_self.store.stage_pending_claim(token_self.mint_claim())
    status = token_self.store.evaluate_pending_claim(key2)
    assert (status["ready"], status["need"]) == (False, 1)


def test_readiness_revalidates_staged_signatures_fail_closed():
    org = Org(requires="admin-ack", token=True)
    claim_key = org.store.stage_pending_claim(org.mint_claim())
    body = org.store.get_pending_claim(claim_key)["body"]
    org.store.add_pending_approval(
        claim_key, sign_approval(org.admin, "member.claim", body)
    )
    # Tamper the staged approvals blob directly in the DB.
    import json as _json

    doctored = [{"key": org.admin.public_hex, "sig": "ab" * 64}]
    with org.store.db:
        org.store.db.execute(
            "UPDATE ledger_pending_claims SET approvals = ? WHERE claim_key = ?",
            (_json.dumps(doctored).encode(), claim_key),
        )
    with pytest.raises(SignatureError):
        org.store.evaluate_pending_claim(claim_key)
    with pytest.raises(StoreError):
        org.store.evaluate_pending_claim("00" * 32)


def test_admitting_is_a_need_sized_deterministic_subset():
    """MAX_APPROVALS bounding: however many authorized approvers sign,
    finalization re-mints with EXACTLY the need-sized admitting subset."""
    org = Org(requires="admin-ack", threshold=1, token=True)
    admin2 = KeyPair.generate()
    org._emit(
        org.root,
        {
            "type": "delegate",
            "child_pub": admin2.public_hex,
            "scope": ["role:grant:member"],
            "can_redelegate": False,
            "proof": sign_delegate_proof(
                admin2, org.genesis_id, org.root.public_hex,
                ["role:grant:member"],
            ),
        },
    )
    claim_key = org.store.stage_pending_claim(org.mint_claim())
    body = org.store.get_pending_claim(claim_key)["body"]
    merged = org.store.add_pending_approval(
        claim_key, sign_approval(org.admin, "member.claim", body)
    )
    merged = org.store.add_pending_approval(
        claim_key, sign_approval(admin2, "member.claim", body)
    )
    status = org.store.evaluate_pending_claim(claim_key)
    assert (status["ready"], status["have"], status["need"]) == (True, 2, 1)
    expected = sorted([org.admin.public_hex, admin2.public_hex])[:1]
    assert status["admitting"] == expected  # deterministic first-need subset

    # Finalize with ONLY the admitting subset: folds admitted.
    subset = [e for e in merged if e["key"] in status["admitting"]]
    final, _ = mint_member_claim(
        org.seed, org.genesis_id, invite_ref=org.invite_id,
        heads=org.store.heads(), hlc=org.next_hlc(),
        token=org.token, approvals=subset,
    )
    assert org.store.evaluate_claim(final) is None
    org.store.append(final)
    assert org.persona.public_hex in org.store.fold().members


# -- cz4fb: the claim must survive approval latency past the invite expiry ----------


def test_finalize_survives_approval_outlasting_the_invite():
    """A claim that entered BEFORE expiry stays admittable however long
    approval takes — by re-minting at its stored causal position, which
    is the only L1-compatible way to record 'redeemed before expiry'
    (the pending row is off-ledger state the fold cannot see)."""
    org = Org(requires="admin-ack", token=True)
    expiry = org.store.get(org.invite_id).payload["expiry"]

    # Submit while the invite is live; it stages.
    claim = org.mint_claim()
    assert claim.hlc.ts < expiry
    assert org.store.evaluate_claim(claim) == R_APPROVAL_MISSING
    claim_key = org.store.stage_pending_claim(claim)

    # Org activity pushes the heads well past the invite's expiry.
    for i in range(3):
        org._ts = expiry + 600_000 * (i + 1)
        filler = KeyPair.generate()
        org._emit(
            org.root,
            {
                "type": "delegate",
                "child_pub": filler.public_hex,
                "scope": ["link:publish"],
                "can_redelegate": False,
                "proof": sign_delegate_proof(
                    filler, org.genesis_id, org.root.public_hex,
                    ["link:publish"],
                ),
            },
        )
    record = org.store.get_pending_claim(claim_key)
    merged = org.store.add_pending_approval(
        claim_key, sign_approval(org.admin, "member.claim", record["body"])
    )
    status = org.store.evaluate_pending_claim(claim_key)
    assert status["ready"] is True

    # Finalizing at the CURRENT frontier is refused — the gap this fixes.
    stale, _ = mint_member_claim(
        org.seed, org.genesis_id, invite_ref=org.invite_id,
        heads=org.store.heads(), hlc=org.next_hlc(),
        token=org.token, approvals=merged,
    )
    assert org.store.evaluate_claim(stale) == R_INVITE_EXPIRED

    # Finalizing at the STORED position admits.
    position = status["position"]
    pinned, _ = mint_member_claim(
        org.seed, org.genesis_id, invite_ref=org.invite_id,
        heads=position["parents"], hlc=HLC(*position["hlc"]),
        token=org.token, approvals=merged,
    )
    assert org.store.evaluate_claim(pinned) is None
    org.store.append(pinned)
    assert org.persona.public_hex in org.store.fold().members


def test_a_first_submission_after_expiry_is_still_refused():
    """Pinning must not weaken the gate for a genuinely late claim."""
    org = Org(requires="admin-ack", token=True)
    expiry = org.store.get(org.invite_id).payload["expiry"]
    org._ts = expiry + 60_000
    late, _ = mint_member_claim(
        org.seed, org.genesis_id, invite_ref=org.invite_id,
        heads=org.store.heads(), hlc=HLC(expiry + 120_000),
        token=org.token,
    )
    assert org.store.evaluate_claim(late) == R_INVITE_EXPIRED


def test_pending_claim_ttl_is_a_distinct_terminal_state():
    org = Org(requires="admin-ack", token=True)
    staged_at = 1_800_000_000_000
    claim_key = org.store.stage_pending_claim(org.mint_claim(), now=staged_at)
    within = org.store.evaluate_pending_claim(
        claim_key, now=staged_at + PENDING_CLAIM_TTL_MS - 1
    )
    assert within["reason"] == R_APPROVAL_MISSING  # still ordinary pending
    beyond = org.store.evaluate_pending_claim(
        claim_key, now=staged_at + PENDING_CLAIM_TTL_MS + 1
    )
    assert (beyond["ready"], beyond["reason"]) == (False, "claim-expired")
    assert beyond["position"] is None  # nothing to finalize at


def test_legacy_staging_row_reports_itself():
    """A row staged before the migration has no causal position; it must
    say so rather than let a client finalize at the wrong one."""
    org = Org(requires="admin-ack", token=True)
    claim_key = org.store.stage_pending_claim(org.mint_claim())
    with org.store.db:
        org.store.db.execute(
            "UPDATE ledger_pending_claims SET parents = NULL WHERE claim_key = ?",
            (claim_key,),
        )
    verdict = org.store.evaluate_pending_claim(claim_key)
    assert (verdict["ready"], verdict["reason"]) == (False, "legacy-staging")
    assert verdict["position"] is None


def test_position_survives_reopen(tmp_path):
    db = tmp_path / "org.db"
    org = Org(path=db, requires="admin-ack", token=True)
    claim = org.mint_claim()
    claim_key = org.store.stage_pending_claim(claim)
    org.store.close()
    with LedgerStore(db) as reopened:
        record = reopened.get_pending_claim(claim_key)
        assert record["parents"] == sorted(claim.parents)
        assert record["hlc_count"] == claim.hlc.count
        assert record["staged_at"] is not None
