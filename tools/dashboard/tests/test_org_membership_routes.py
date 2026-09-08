"""The Membership screen's read model (auto-z9ztt, graph://9282a825-4ce).

A real founded ledger backs every case: founding via found_org_ledger gives a
member, an owner role_def, and a claimed founding invite; the tests add live,
expired, and bearer-claimed states on top and read them all back through the
one projection route.
"""
from __future__ import annotations

import hashlib
import time

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from tools.dashboard import org_membership_routes
from tools.network.idkit import KeyPair
from tools.network.idkit.persona import derive_persona
from tools.network.ledger.events import HLC, make_event
from tools.network.ledger.found import found_org_ledger
from tools.network.ledger import store as ledger_store_module
from tools.network.ledger.store import LedgerStore


NOW_MS = 1_777_000_000_000
PERSONAL_SEED = b"\x11" * 32
JOINER_SEED = b"\x22" * 32
BEARER = "ab" * 32


@pytest.fixture
def founded(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ledger_store_module, "org_ledger_db_path",
        lambda slug, root=None: tmp_path / f"{slug}.db",
    )
    monkeypatch.setattr(
        org_membership_routes.api_auth,
        "require_global_api_authority",
        lambda request: None,
    )
    monkeypatch.setattr(org_membership_routes, "_member_profiles", lambda slug: {})
    monkeypatch.setattr(org_membership_routes, "_link_grants", lambda slug: {})
    from tools.graph import org_ops

    monkeypatch.setattr(org_ops, "persona_pub_for_org", lambda genesis: None)
    store = LedgerStore(tmp_path / "testorg.db")
    record = found_org_ledger(
        store,
        org_id="11111111-1111-4111-8111-111111111111",
        org_root=KeyPair.generate(),
        personal_root_seed=PERSONAL_SEED,
        now=NOW_MS,
    )
    founder = derive_persona(PERSONAL_SEED, record.genesis_id)
    yield store, record, founder
    store.db.close()


def _client() -> TestClient:
    return TestClient(Starlette(routes=org_membership_routes.ROUTES))


def _bearer_invite(store, founder, *, expiry, hlc, max_uses=None):
    payload = {
        "type": "invite",
        "granted_role": "owner",
        "expiry": expiry,
        "sponsor": founder.public_hex,
        "token_hash": hashlib.sha256(BEARER.encode("utf-8")).hexdigest(),
    }
    if max_uses is not None:
        payload["max_uses"] = max_uses
    return store.append(make_event(
        founder, payload, list(store.heads()), HLC(*hlc),
    ))


def test_unfounded_org_reports_founded_false(founded):
    response = _client().get("/api/orgs/neverfounded/membership")
    assert response.status_code == 200
    assert response.json() == {"founded": False}


def test_founded_ledger_projects_members_roles_and_claimed_invite(founded):
    _store, record, _founder = founded
    body = _client().get("/api/orgs/testorg/membership").json()
    assert body["founded"] is True
    assert body["genesis_id"] == record.genesis_id
    assert body["org_uuid"] == "11111111-1111-4111-8111-111111111111"
    [member] = body["members"]
    assert member["persona"] == record.founder_persona_pub
    assert member["roles"] == ["owner"]
    assert member["display_name"] is None
    assert member["avatar"] is None
    assert member["color"] is None
    [role] = body["role_defs"]
    assert role["name"] == "owner"
    assert role["claim_requires"] == "self"
    assert role["approver_threshold"] == 1
    [invite] = body["invites"]
    assert invite["invite_id"] == record.founding_invite_id
    assert invite["status"] == "claimed"
    assert invite["binding"] == "key"
    assert invite["uses"] == {"max_uses": 1, "used": 1, "remaining": 0}
    assert body["pending_claims"] == []
    assert body["viewer_persona"] is None


def test_invite_expiry_is_judged_by_the_server_clock(founded):
    store, _record, founder = founded
    live_id = _bearer_invite(
        store, founder, expiry=int(time.time() * 1000) + 86_400_000,
        hlc=(NOW_MS + 1_000, 0),
    )
    expired_id = _bearer_invite(
        store, founder, expiry=NOW_MS + 1, hlc=(NOW_MS + 2_000, 0),
    )
    invites = {
        row["invite_id"]: row
        for row in _client().get("/api/orgs/testorg/membership").json()["invites"]
    }
    assert invites[live_id]["status"] == "live"
    assert invites[live_id]["binding"] == "bearer"
    assert invites[live_id]["granted_role"] == "owner"
    assert invites[expired_id]["status"] == "expired"


def test_join_url_joins_by_invite_ref_and_carries_no_bearer(founded, monkeypatch):
    store, _record, founder = founded
    live_id = _bearer_invite(
        store, founder, expiry=int(time.time() * 1000) + 86_400_000,
        hlc=(NOW_MS + 1_000, 0),
    )
    monkeypatch.setattr(
        org_membership_routes, "_link_grants",
        lambda slug: {live_id: {
            "url": "https://relay.example/l/" + "cd" * 16,
            "label": "Dean's invite",
        }},
    )
    body = _client().get("/api/orgs/testorg/membership").json()
    invites = {row["invite_id"]: row for row in body["invites"]}
    assert invites[live_id]["join_url"] == "https://relay.example/l/" + "cd" * 16
    assert invites[live_id]["label"] == "Dean's invite"
    # Mint-side secrecy: the bearer exists only in the minting browser. It
    # must never appear on invite rows (the pending-claim body is different:
    # a claimant SUBMITS its token to this operator, and countersignatures
    # verify over those exact bytes).
    import json as _json

    assert BEARER not in _json.dumps(body["invites"])


def test_staged_bearer_claim_surfaces_progress_and_signed_profile(founded):
    store, record, founder = founded
    live_id = _bearer_invite(
        store, founder, expiry=int(time.time() * 1000) + 86_400_000,
        hlc=(NOW_MS + 1_000, 0),
    )
    joiner = derive_persona(JOINER_SEED, record.genesis_id)
    claim = make_event(
        joiner,
        {
            "type": "member.claim",
            "invite_ref": live_id,
            "persona_pub": joiner.public_hex,
            "profile": {"display_name": "Dean"},
            "approvals": [],
            "token": BEARER,
        },
        list(store.heads()),
        HLC(NOW_MS + 3_000, 0),
    )
    assert store.evaluate_claim(claim) == "approval-missing"
    # Stage at the REAL clock: the route's readiness pass applies the 7-day
    # staging TTL against server time, and a fixture-epoch staged_at would
    # (correctly) age out as claim-expired.
    store.stage_pending_claim(claim, now=int(time.time() * 1000))

    body = _client().get("/api/orgs/testorg/membership").json()
    [pending] = body["pending_claims"]
    assert pending["persona_pub"] == joiner.public_hex
    assert pending["invite_label"] is None
    assert pending["invite_binding"] == "bearer"
    assert pending["invite_ref"] == live_id
    assert pending["granted_role"] == "owner"
    assert pending["introduction"] == {"display_name": "Dean"}
    assert pending["have"] == 0
    assert pending["need"] == 1
    assert pending["ready"] is False
    # The staged body is deliberately NOT served (autonomy@24ee8ae): a
    # token-bound claim carries the invitation's bearer in body["token"],
    # and countersigning needs only the invite_ref and persona_pub. The
    # projection must not hand a caller the secret it does not need.
    assert "body" not in pending


def test_authority_refusal_short_circuits(founded, monkeypatch):
    monkeypatch.setattr(
        org_membership_routes.api_auth,
        "require_global_api_authority",
        lambda request: JSONResponse({"error": "denied"}, status_code=403),
    )
    response = _client().get("/api/orgs/testorg/membership")
    assert response.status_code == 403


def test_member_rows_carry_the_directory_presentation(founded, monkeypatch):
    """Members pull display name, avatar, and color from the central
    member-profile set, keyed by persona."""
    _store, record, _founder = founded
    monkeypatch.setattr(
        org_membership_routes, "_member_profiles",
        lambda slug: {record.founder_persona_pub: {
            "display_name": "Jeremy", "avatar": "4fde3638-009",
            "color": "#0f766e",
        }},
    )
    [member] = _client().get("/api/orgs/testorg/membership").json()["members"]
    assert member["display_name"] == "Jeremy"
    assert member["avatar"] == "4fde3638-009"
    assert member["color"] == "#0f766e"


def test_multi_use_invite_reports_capacity(founded):
    """A max_uses invite stays live with remaining capacity exposed
    (autonomy@f60c26a): the row renders "0 of 3 used" honestly."""
    store, _record, founder = founded
    multi_id = _bearer_invite(
        store, founder, expiry=int(time.time() * 1000) + 86_400_000,
        hlc=(NOW_MS + 1_000, 0), max_uses=3,
    )
    invites = {
        row["invite_id"]: row
        for row in _client().get("/api/orgs/testorg/membership").json()["invites"]
    }
    assert invites[multi_id]["status"] == "live"
    assert invites[multi_id]["uses"] == {"max_uses": 3, "used": 0, "remaining": 3}


def test_pending_row_names_the_link_it_came_in_on(founded, monkeypatch):
    store, record, founder = founded
    live_id = _bearer_invite(
        store, founder, expiry=int(time.time() * 1000) + 86_400_000,
        hlc=(NOW_MS + 1_000, 0),
    )
    monkeypatch.setattr(
        org_membership_routes, "_link_grants",
        lambda slug: {live_id: {"url": "https://x/l/" + "cd" * 16, "label": "Dean's invite"}},
    )
    joiner = derive_persona(JOINER_SEED, record.genesis_id)
    claim = make_event(
        joiner,
        {
            "type": "member.claim", "invite_ref": live_id,
            "persona_pub": joiner.public_hex, "profile": {},
            "approvals": [], "token": BEARER,
        },
        list(store.heads()), HLC(NOW_MS + 3_000, 0),
    )
    store.stage_pending_claim(claim, now=int(time.time() * 1000))
    [pending] = _client().get("/api/orgs/testorg/membership").json()["pending_claims"]
    assert pending["invite_label"] == "Dean's invite"
