"""The membership projection's role fields: version, holders, what the
viewer may do with each role, the unassemblable-threshold warning, and the
org-key owner the browser compares against to decide whether "Define role"
can run here. Roles design of record graph://d1b3db8f-879, bead auto-ai7li.
"""
from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard import org_membership_routes
from tools.network.idkit import KeyPair
from tools.network.idkit.persona import derive_persona
from tools.network.ledger.events import HLC, make_event
from tools.network.ledger.found import found_org_ledger
from tools.network.ledger import store as ledger_store_module
from tools.network.ledger.store import LedgerStore

NOW_MS = 1_777_000_000_000
PERSONAL_SEED = b"\x33" * 32


@pytest.fixture
def founded(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ledger_store_module, "org_ledger_db_path",
        lambda slug, root=None: tmp_path / f"{slug}.db",
    )
    monkeypatch.setattr(
        org_membership_routes.api_auth, "require_global_api_authority",
        lambda request: None,
    )
    monkeypatch.setattr(org_membership_routes, "_member_profiles", lambda slug: {})
    monkeypatch.setattr(org_membership_routes, "_link_grants", lambda slug: {})
    monkeypatch.setattr(org_membership_routes, "_org_key_owner_kem_pub", lambda slug: None)
    from tools.graph import org_ops
    monkeypatch.setattr(org_ops, "persona_pub_for_org", lambda genesis: None)

    root = KeyPair.generate()
    store = LedgerStore(tmp_path / "testorg.db")
    record = found_org_ledger(
        store, org_id="11111111-1111-4111-8111-111111111112",
        org_root=root, personal_root_seed=PERSONAL_SEED, now=NOW_MS,
    )
    founder = derive_persona(PERSONAL_SEED, record.genesis_id)
    yield store, record, founder, root
    store.db.close()


def _client() -> TestClient:
    return TestClient(Starlette(routes=org_membership_routes.ROUTES))


def _define(store, root, name, scope_set=(), threshold=None, *, tick):
    payload = {
        "type": "role.define", "name": name, "scope_set": sorted(scope_set),
        "claim_requires": "admin-ack", "version": 1,
    }
    if threshold is not None:
        payload["approver_threshold"] = {"kind": "static", "count": threshold}
    return store.append(make_event(root, payload, list(store.heads()), HLC(NOW_MS + tick, 0)))


def _roles(body):
    return {role["name"]: role for role in body["role_defs"]}


def test_role_defs_carry_version_holders_and_viewer_authority(founded, monkeypatch):
    store, record, founder, root = founded
    _define(store, root, "member", tick=1000)
    bare = KeyPair.generate().public_hex
    store.append(make_event(
        root, {"type": "role.grant", "persona": bare, "role": "member"},
        list(store.heads()), HLC(NOW_MS + 2000, 0),
    ))

    # An anonymous viewer (persona unresolved) may neither invite nor grant.
    body = _client().get("/api/orgs/testorg/membership").json()
    roles = _roles(body)
    assert roles["owner"]["version"] == 1
    assert roles["owner"]["holders"] == [founder.public_hex]
    assert roles["member"]["holders"] == []
    assert roles["member"]["bare_holders"] == [bare]
    assert roles["member"]["minter_may_invite"] is False
    assert roles["member"]["viewer_may_grant"] is False
    assert roles["member"]["threshold_warning"] is None

    # The founder (owner, holds *) may invite for and grant every role.
    from tools.graph import org_ops
    monkeypatch.setattr(org_ops, "persona_pub_for_org", lambda genesis: founder.public_hex)
    body = _client().get("/api/orgs/testorg/membership").json()
    roles = _roles(body)
    assert body["viewer_persona"] == founder.public_hex
    assert roles["member"]["minter_may_invite"] is True
    assert roles["member"]["viewer_may_grant"] is True
    assert roles["owner"]["minter_may_invite"] is True


def test_an_unassemblable_threshold_is_a_warning_not_a_refusal(founded):
    store, _record, _founder, root = founded
    # Admin needs two approvers; only the founder could approve (root is not
    # counted), so the definition stands and the projection warns.
    _define(store, root, "admin", ["invite:member"], threshold=2, tick=1000)
    body = _client().get("/api/orgs/testorg/membership").json()
    admin = _roles(body)["admin"]
    assert admin["approver_threshold"] == 2
    warning = admin["threshold_warning"]
    assert warning is not None and warning["approver_threshold"] == 2
    assert len(warning["admission_authority_holders"]) < 2


def test_org_key_owner_is_surfaced_for_the_browser_to_compare(founded, monkeypatch):
    monkeypatch.setattr(
        org_membership_routes, "_org_key_owner_kem_pub", lambda slug: "ab" * 32,
    )
    body = _client().get("/api/orgs/testorg/membership").json()
    assert body["org_key_owner_kem_pub"] == "ab" * 32
