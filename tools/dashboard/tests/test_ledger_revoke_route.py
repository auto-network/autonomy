"""POST /api/network/ledger/revoke (auto-j1833): deactivate an invitation.

Real founded ledgers back every case; the trial-fold gate is what keeps an
unauthorized revoke OUT of the ledger instead of appended as an invalid row.
"""
from __future__ import annotations

import hashlib

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard import network_routes
from tools.network.idkit import KeyPair
from tools.network.idkit.persona import derive_persona
import tools.network.ledger as ledger_package
from tools.network.ledger.events import HLC, make_event
from tools.network.ledger.found import found_org_ledger
from tools.network.ledger import store as ledger_store_module
from tools.network.ledger.store import LedgerStore


NOW_MS = 1_777_000_000_000
SEED = b"\x33" * 32
OTHER_SEED = b"\x44" * 32


@pytest.fixture
def founded(tmp_path, monkeypatch):
    def _path(slug, root=None):
        return tmp_path / f"{slug}.db"
    # The handler imports the package-level re-export; patch both namespaces.
    monkeypatch.setattr(ledger_store_module, "org_ledger_db_path", _path)
    monkeypatch.setattr(ledger_package, "org_ledger_db_path", _path)
    monkeypatch.setattr(network_routes, "_mock_mode", lambda: False)
    monkeypatch.setattr(
        network_routes, "resolve_scoped_org",
        lambda org, request=None: (org, None),
    )
    store = LedgerStore(tmp_path / "testorg.db")
    record = found_org_ledger(
        store,
        org_id="11111111-1111-4111-8111-111111111111",
        org_root=KeyPair.generate(),
        personal_root_seed=SEED,
        now=NOW_MS,
    )
    founder = derive_persona(SEED, record.genesis_id)
    invite_id = store.append(make_event(
        founder,
        {
            "type": "invite",
            "granted_role": "owner",
            "expiry": NOW_MS + 999_999_999,
            "sponsor": founder.public_hex,
            "token_hash": hashlib.sha256(b"bearer").hexdigest(),
        },
        list(store.heads()),
        HLC(NOW_MS + 1_000, 0),
    ))
    yield store, record, founder, invite_id
    store.db.close()


def _client() -> TestClient:
    return TestClient(Starlette(routes=network_routes.ROUTES))


def _revoke_wire(store, author, target_event, *, parents=None):
    event = make_event(
        author,
        {"type": "revoke", "target_event": target_event},
        list(store.heads()) if parents is None else parents,
        HLC(NOW_MS + 5_000, 0),
    )
    return event.to_json().decode("utf-8")


def test_sponsor_deactivates_own_invitation(founded):
    store, _record, founder, invite_id = founded
    response = _client().post("/api/network/ledger/revoke", json={
        "org": "testorg", "event": _revoke_wire(store, founder, invite_id),
    })
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    # The route wrote through its own store handle; fold a fresh one.
    with LedgerStore(store.path) as fresh:
        assert fresh.fold().invites[invite_id] == "revoked"


def test_unauthorized_persona_is_refused_and_nothing_appends(founded):
    store, record, _founder, invite_id = founded
    stranger = derive_persona(OTHER_SEED, record.genesis_id)
    before = len(store.events())
    response = _client().post("/api/network/ledger/revoke", json={
        "org": "testorg", "event": _revoke_wire(store, stranger, invite_id),
    })
    assert response.status_code == 403
    assert "revoke-unauthorized" in response.json()["error"]
    assert len(store.events()) == before, "an invalid revoke must never append"


def test_stale_heads_get_a_retryable_409(founded):
    store, _record, founder, invite_id = founded
    wire = _revoke_wire(store, founder, invite_id, parents=[store.ledger.genesis_id])
    response = _client().post("/api/network/ledger/revoke", json={
        "org": "testorg", "event": wire,
    })
    assert response.status_code == 409


def test_non_invite_targets_are_refused(founded):
    store, record, founder, _invite_id = founded
    response = _client().post("/api/network/ledger/revoke", json={
        "org": "testorg",
        "event": _revoke_wire(store, founder, record.founder_claim_id),
    })
    assert response.status_code == 400
    assert "not an invitation" in response.json()["error"]
