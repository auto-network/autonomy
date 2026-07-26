"""The membership claim flow, end to end over the org:join channel path.

The acceptance the claim protocol actually needs, with no web page: a
REAL founded org (found_org_ledger), a REAL bearer invite, a REAL
org:join link-grant Setting (so check_grant's I9 gate is the actual
gate), the REAL connector handler, and the REAL claim_service. The only
double is the transport — the test calls ``handler(token, message)``
with exactly the bytes the connector hands it, because the relay routes
opaque frames and is provably outside the trust path.

Covers the full protocol walk, the negative walks through that same
real path, and the bearer-safety invariant end to end (a token-bound
claim never admits on submit alone — proven here through the shipped
service and dispatch, not only at the fold).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os

import pytest

from tools.dashboard import claim_service, link_serving
from tools.graph import settings_ops
from tools.graph.schemas.network_identity import (
    NETWORK_LINK_GRANT_REVISION,
    NETWORK_LINK_GRANT_SET_ID,
)
from tools.network.idkit import KeyPair, derive_persona, generate_token
from tools.network.ledger import HLC, LedgerStore, make_event, org_ledger_db_path, sign_approval
from tools.network.ledger.claims import mint_member_claim
from tools.network.ledger.found import found_org_ledger

ORG = "claimorg"
ORG_ID = "018f6b2a-7c4d-7e11-8a3b-9d5c1e2f4a6b"
TOKEN = "ab" * 16  # relay link tokens are 32 hex chars
T0 = 1_800_000_000_000
FAR = T0 + 10**9


class World:
    """A founded org with a bearer invite and a live org:join grant."""

    def __init__(self, tmp_path):
        from tools.graph.db import GraphDB

        GraphDB.create_org_db(ORG, root=tmp_path).close()
        self.store = LedgerStore(org_ledger_db_path(ORG))
        self.root = KeyPair.generate()
        self.owner_seed = os.urandom(32)
        self.founded = found_org_ledger(
            self.store, org_id=ORG_ID, org_root=self.root,
            personal_root_seed=self.owner_seed, now=T0,
        )
        self._ts = T0 + 10_000
        # An admin-ack role with threshold 1, and a delegated approver.
        self._emit(self.root, {
            "type": "role.define", "name": "member", "scope_set": ["link:publish"],
            "claim_requires": "admin-ack", "version": 1,
        })
        self.admin = KeyPair.generate()
        self._emit(self.root, {
            "type": "delegate", "child_pub": self.admin.public_hex,
            "scope": ["role:grant:member"], "can_redelegate": False,
        })
        self.token = generate_token()
        self.invite_ref = self._emit(self.root, {
            "type": "invite", "granted_role": "member", "expiry": FAR,
            "sponsor": self.root.public_hex,
            "token_hash": hashlib.sha256(self.token.encode()).hexdigest(),
        })
        self.store.close()
        self._publish_grant()
        self.invitee_seed = os.urandom(32)
        self.persona = derive_persona(self.invitee_seed, self.founded.genesis_id)

    def _emit(self, author, payload):
        self._ts += 1_000
        return self.store.append(
            make_event(author, payload, sorted(self.store.heads()), HLC(self._ts))
        )

    def _publish_grant(self):
        """The REAL local grant row the I9 gate reads (never a stub)."""
        settings_ops.add_setting(
            NETWORK_LINK_GRANT_SET_ID, NETWORK_LINK_GRANT_REVISION, TOKEN,
            {
                "token": TOKEN,
                "target_uuid": ORG_ID,
                "target_type": "org:join",
                "invite_ref": self.invite_ref,
                "subject": {"kind": "operator", "id": "join-issuer"},
                "url": f"https://relay.auto.network/l/{TOKEN}",
                "issued_at": "2026-07-26T00:00:00Z",
                "meta": {},
            },
            org=ORG, state="canonical",
        )

    # -- the transport seam: exactly the bytes the connector hands over ----

    def channel(self, request: dict, token: str = TOKEN) -> dict | bytes:
        handler = link_serving.make_grant_handler(ORG)
        raw = asyncio.run(handler(token, json.dumps(request).encode()))
        if raw in (link_serving.REFUSED, link_serving.BAD_REQUEST):
            return raw
        return json.loads(raw.split(b"\n", 1)[0])

    def mint(self, context: dict, approvals=(), position=None):
        """Mint as claim.js does, optionally at the staged fixed position."""
        if position is None:
            heads = context["heads"]
            ts, count = context["max_hlc"]
            hlc = HLC(ts + 1_000, 0) if count is not None else HLC(ts + 1_000)
        else:
            heads = position["parents"]
            hlc = HLC.from_value(position["hlc"])
        event, _ = mint_member_claim(
            self.invitee_seed, context["genesis_id"],
            invite_ref=self.invite_ref, heads=heads, hlc=hlc,
            token=self.token, approvals=approvals,
        )
        return event

    def advance_heads_past_invite_expiry(self):
        """Make current-frontier finalization late without expiring wall time."""
        with LedgerStore(org_ledger_db_path(ORG)) as store:
            store.append(make_event(
                self.root,
                {
                    "type": "delegate",
                    "child_pub": KeyPair.generate().public_hex,
                    "scope": ["link:publish"],
                    "can_redelegate": False,
                },
                store.heads(),
                HLC(FAR + 1_000),
            ))

    def members(self):
        with LedgerStore(org_ledger_db_path(ORG)) as store:
            return store.fold().members


@pytest.fixture
def world(tmp_path, monkeypatch):
    from tools.graph.db import GraphDB

    GraphDB.close_all_pooled()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path))
    monkeypatch.delenv("GRAPH_DB", raising=False)  # real per-org settings stores
    GraphDB.create_org_db("personal", type_="personal", root=tmp_path).close()
    yield World(tmp_path)
    GraphDB.close_all_pooled()


# -- the full protocol walk -------------------------------------------------------


def test_full_claim_flow_over_the_join_channel(world):
    # 1. context — the invitee learns where and what to mint against.
    context = world.channel({"v": 1, "op": "context"})
    assert context["status"] == "ok"
    assert context["genesis_id"] == world.founded.genesis_id
    assert context["granted_role"] == "member"
    assert context["binding"] == "token"
    assert context["invite_expiry"] == FAR

    # 2. submit — bearer safety: a token-bound claim STAGES, never admits.
    claim = world.mint(context)
    pending = world.channel({
        "v": 1, "op": "submit", "event": claim.to_json().decode("utf-8"),
    })
    assert pending["status"] == "pending"
    assert (pending["have"], pending["need"]) == (0, 1)
    assert world.persona.public_hex not in world.members()

    # It is a STAGING row, not a ledger event.
    with LedgerStore(org_ledger_db_path(ORG)) as store:
        claim_key = store.claim_key(world.invite_ref, world.persona.public_hex)
        assert store.get_pending_claim(claim_key) is not None
        assert claim.event_id not in store.ledger

    # 3. status — pending, over the channel.
    status = world.channel({
        "v": 1, "op": "status", "persona_pub": world.persona.public_hex,
    })
    assert (status["status"], status["have"], status["need"]) == ("pending", 0, 1)

    # 4. countersign — the org-internal approver path (not a channel op).
    with LedgerStore(org_ledger_db_path(ORG)) as store:
        body = store.get_pending_claim(claim_key)["body"]
    ready = claim_service.countersign(
        ORG, world.invite_ref, world.persona.public_hex,
        sign_approval(world.admin, "member.claim", body),
    )
    assert ready["status"] == "ready"
    assert (ready["have"], ready["need"]) == (1, 1)
    assert ready["admitting"] == [world.admin.public_hex]

    # Approval latency advances the org beyond the invite's causal expiry.
    # A current-frontier re-mint remains rejected; the server-supplied
    # position is the only valid finalize position.
    world.advance_heads_past_invite_expiry()
    current_context = world.channel({"v": 1, "op": "context"})

    # 5. finalize — re-mint with EXACTLY the admitting subset at the
    # server-supplied staging position, then submit again.
    with LedgerStore(org_ledger_db_path(ORG)) as store:
        approvals = [
            e for e in store.get_pending_claim(claim_key)["approvals"]
            if e["key"] in ready["admitting"]
        ]
    current_frontier = world.mint(current_context, approvals=approvals)
    assert world.channel({
        "v": 1, "op": "submit",
        "event": current_frontier.to_json().decode("utf-8"),
    }) == {"v": 1, "status": "rejected", "reason": "invite-expired"}

    final = world.mint(
        current_context,
        approvals=approvals,
        position=ready["position"],
    )
    admitted = world.channel({
        "v": 1, "op": "submit", "event": final.to_json().decode("utf-8"),
    })
    assert admitted["status"] == "admitted"

    # 6. the member is real, and the staging row is gone.
    member = world.members()[world.persona.public_hex]
    assert member.roles == ("member",)
    assert member.invite_id == world.invite_ref
    with LedgerStore(org_ledger_db_path(ORG)) as store:
        assert store.get_pending_claim(claim_key) is None
    assert world.channel({
        "v": 1, "op": "status", "persona_pub": world.persona.public_hex,
    })["status"] == "admitted"


def test_status_is_absent_before_any_submit(world):
    assert world.channel({
        "v": 1, "op": "status", "persona_pub": world.persona.public_hex,
    })["status"] == "absent"


# -- negative walks through the same real path -------------------------------------


def test_unauthorized_countersignature_is_rejected_and_not_merged(world):
    context = world.channel({"v": 1, "op": "context"})
    world.channel({
        "v": 1, "op": "submit", "event": world.mint(context).to_json().decode("utf-8"),
    })
    with LedgerStore(org_ledger_db_path(ORG)) as store:
        claim_key = store.claim_key(world.invite_ref, world.persona.public_hex)
        body = store.get_pending_claim(claim_key)["body"]
    stranger = KeyPair.generate()
    verdict = claim_service.countersign(
        ORG, world.invite_ref, world.persona.public_hex,
        sign_approval(stranger, "member.claim", body),
    )
    assert verdict["status"] == "rejected"
    with LedgerStore(org_ledger_db_path(ORG)) as store:
        assert store.get_pending_claim(claim_key)["approvals"] == []  # not merged
    assert world.channel({
        "v": 1, "op": "status", "persona_pub": world.persona.public_hex,
    })["have"] == 0


def test_wrong_token_is_a_hard_reject_and_never_staged(world):
    context = world.channel({"v": 1, "op": "context"})
    event, _ = mint_member_claim(
        world.invitee_seed, context["genesis_id"], invite_ref=world.invite_ref,
        heads=context["heads"], hlc=HLC(context["max_hlc"][0] + 1_000),
        token="ff" * 32,
    )
    verdict = world.channel({
        "v": 1, "op": "submit", "event": event.to_json().decode("utf-8"),
    })
    assert verdict["status"] == "rejected"
    with LedgerStore(org_ledger_db_path(ORG)) as store:
        assert store.get_pending_claim(
            store.claim_key(world.invite_ref, world.persona.public_hex)
        ) is None


def test_foreign_invite_ref_hits_the_anti_enumeration_refusal(world):
    """The channel is scoped to its grant's invitation (defence in depth)."""
    context = world.channel({"v": 1, "op": "context"})
    event, _ = mint_member_claim(
        world.invitee_seed, context["genesis_id"], invite_ref="9c" * 32,
        heads=context["heads"], hlc=HLC(context["max_hlc"][0] + 1_000),
        token=world.token,
    )
    assert world.channel({
        "v": 1, "op": "submit", "event": event.to_json().decode("utf-8"),
    }) == link_serving.REFUSED


def test_unknown_token_never_reveals_the_org(world):
    assert world.channel({"v": 1, "op": "context"}, token="cd" * 16) == (
        link_serving.REFUSED
    )


def test_join_channel_refuses_content_ops(world):
    for op in ("fetch", "head"):
        assert world.channel({"v": 1, "op": op}) == link_serving.REFUSED


# -- bearer safety, end to end through the shipped stack ---------------------------


def test_bearer_claim_cannot_admit_without_a_countersignature(world):
    """The invariant, proven through the real service and dispatch: the
    invitee holding a VALID token still cannot self-admit, and repeated
    submits never promote the claim."""
    context = world.channel({"v": 1, "op": "context"})
    wire = world.mint(context).to_json().decode("utf-8")
    for _ in range(3):
        verdict = world.channel({"v": 1, "op": "submit", "event": wire})
        assert verdict["status"] == "pending"
    assert world.persona.public_hex not in world.members()
