"""Retaining an invitation's bearer — the route stores nothing it has not proved.

Decision of record graph://e75ebdde-6df: the organization keeps an org:join
link's bearer so it can re-render a working link, because possession of the
link buys only the right to ask — admission still needs a countersignature.
The route therefore hashes the submitted token and refuses unless it equals
the invite event's own `token_hash`. Bead auto-2t101.
"""
from __future__ import annotations

import hashlib
import time

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from tools.dashboard import network_routes
from tools.graph import settings_ops
from tools.graph.db import GraphDB
from tools.graph.schemas.network_identity import (
    NETWORK_LINK_GRANT_REVISION,
    NETWORK_LINK_GRANT_SET_ID,
    NETWORK_TOKEN_HEX_LEN,
)
from tools.network.idkit import KeyPair
from tools.network.idkit.persona import derive_persona
from tools.network.ledger import HLC, LedgerStore, org_ledger_db_path
from tools.network.ledger.events import make_event
from tools.network.ledger.found import found_org_ledger

ORG = "personal"
SEED = bytes(reversed(range(32)))
BEARER = "7a" * 32
OTHER = "7b" * 32
GRANT_TOKEN = "c" * NETWORK_TOKEN_HEX_LEN


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    monkeypatch.setenv("AUTONOMY_DATA_ROOT", str(tmp_path))
    orgs_dir = tmp_path / "orgs"
    orgs_dir.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.setenv("GRAPH_ORG", ORG)
    monkeypatch.delenv("GRAPH_DB", raising=False)
    app = Starlette(routes=[
        Route("/api/network/ledger/invite/bearer",
              network_routes.post_ledger_invite_bearer, methods=["POST"]),
    ])
    with TestClient(app) as c:
        yield c
    GraphDB.close_all_pooled()


def _founded_with_invite(*, key_bound: bool = False) -> str:
    """Found the org and mint one invitation; returns its event id."""
    GraphDB.create_org_db(ORG).close()
    root = KeyPair.generate()
    with LedgerStore(org_ledger_db_path(ORG)) as store:
        founded = found_org_ledger(
            store, org_id=ORG, org_root=root, personal_root_seed=SEED,
            now=int(time.time() * 1000) - 10_000,
        )
        operator = derive_persona(SEED, founded.genesis_id)
        payload = {
            "type": "invite", "granted_role": "owner",
            "expiry": int(time.time() * 1000) + 86_400_000,
            "sponsor": operator.public_hex,
        }
        if key_bound:
            payload["invite_pub"] = KeyPair.generate().public_hex
        else:
            payload["token_hash"] = hashlib.sha256(BEARER.encode()).hexdigest()
        invite_id = store.append(make_event(
            operator, payload, list(store.heads()), HLC(int(time.time() * 1000), 0)))
    GraphDB.close_all_pooled()
    return invite_id


def _publish(invite_ref: str, *, token: str = GRANT_TOKEN) -> None:
    settings_ops.add_setting(
        NETWORK_LINK_GRANT_SET_ID, NETWORK_LINK_GRANT_REVISION, token,
        {
            "token": token,
            "url": f"https://relay.auto.network/l/{token}",
            "target_uuid": "11111111-1111-4111-8111-111111111111",
            "target_type": "org:join",
            "invite_ref": invite_ref,
            "meta": {},
            "subject": {"kind": "operator", "id": "op-1"},
            "issued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        org=ORG,
    )
    GraphDB.close_all_pooled()


def _stored_bearer(token: str = GRANT_TOKEN):
    members = settings_ops.read_owned_set(
        NETWORK_LINK_GRANT_SET_ID, org=ORG,
        target_revision=NETWORK_LINK_GRANT_REVISION,
    ).members
    for member in members:
        if (member.payload or {}).get("token") == token:
            return (member.payload or {}).get("bearer")
    return None


def _post(client, **body):
    return client.post("/api/network/ledger/invite/bearer", json=body)


def test_the_bearer_is_retained_when_it_proves_out(client):
    """THE ONE THAT MATTERS: a correct bearer is stored, so the row can
    re-render a working link instead of losing it after one showing."""
    invite_ref = _founded_with_invite()
    _publish(invite_ref)

    response = _post(client, org=ORG, invite_ref=invite_ref, token=BEARER)

    assert response.status_code == 200, response.text
    assert response.json()["stored"] is True
    assert _stored_bearer() == BEARER


def test_a_wrong_token_is_refused_and_nothing_is_written(client):
    invite_ref = _founded_with_invite()
    _publish(invite_ref)

    response = _post(client, org=ORG, invite_ref=invite_ref, token=OTHER)

    assert response.status_code == 403
    assert "not this invitation's bearer" in response.json()["error"]
    assert _stored_bearer() is None, "a refused token must not be written"


def test_storing_the_same_bearer_twice_is_idempotent(client):
    invite_ref = _founded_with_invite()
    _publish(invite_ref)
    assert _post(client, org=ORG, invite_ref=invite_ref, token=BEARER).json()["stored"] is True

    again = _post(client, org=ORG, invite_ref=invite_ref, token=BEARER)

    assert again.status_code == 200
    assert again.json()["stored"] is False
    assert _stored_bearer() == BEARER


def test_a_key_bound_invitation_has_no_bearer_to_retain(client):
    invite_ref = _founded_with_invite(key_bound=True)
    _publish(invite_ref)

    response = _post(client, org=ORG, invite_ref=invite_ref, token=BEARER)

    assert response.status_code == 400
    assert "key-bound" in response.json()["error"]
    assert _stored_bearer() is None


def test_an_invitation_with_no_published_link_is_refused(client):
    invite_ref = _founded_with_invite()  # minted, never published

    response = _post(client, org=ORG, invite_ref=invite_ref, token=BEARER)

    assert response.status_code == 404
    assert "no published link" in response.json()["error"]


def test_an_unknown_invitation_is_refused(client):
    _founded_with_invite()

    response = _post(client, org=ORG, invite_ref="ab" * 32, token=BEARER)

    assert response.status_code == 404
    assert "no such invitation" in response.json()["error"]


def test_malformed_input_is_refused_before_anything_is_read(client):
    assert _post(client, invite_ref="ab" * 32, token=BEARER).status_code == 400
    assert _post(client, org=ORG, token=BEARER).status_code == 400
    assert _post(client, org=ORG, invite_ref="nothex", token=BEARER).status_code == 400
    assert _post(client, org=ORG, invite_ref="ab" * 32, token="short").status_code == 400


def test_mock_mode_has_no_ledger(client, monkeypatch):
    monkeypatch.setenv("DASHBOARD_MOCK", "1")
    assert _post(client, org=ORG, invite_ref="ab" * 32, token=BEARER).status_code == 502
