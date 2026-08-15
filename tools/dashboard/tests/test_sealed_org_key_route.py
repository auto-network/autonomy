"""POST /api/network/org-key/sealed — the browser-sealed org root (I1).

The org root is generated and sealed in the operator's browser; only sealed
material crosses the wire. These tests build the sealed payload with the same
primitives the browser uses (the JS sealing module mirrors this Python byte
for byte) and pin the route's contract, including the rule that the key locks
the moment the ledger is founded.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from tools.graph import org_ops, settings_ops
from tools.graph.schemas.network_identity import (
    NETWORK_ORG_KEY_SET_ID,
    ORG_ROOT_ARMOR_PURPOSE,
)
from tools.network.idkit import KeyPair
from tools.network.idkit.sealing import derive_encapsulation_keypair, seal

PERSONAL_SEED = bytes(range(32))


def sealed_payload(org_root: KeyPair | None = None) -> dict:
    """What the browser computes locally and submits."""
    org_root = org_root or KeyPair.generate()
    _, recipient_pub = derive_encapsulation_keypair(
        PERSONAL_SEED, ORG_ROOT_ARMOR_PURPOSE
    )
    record = seal(
        bytes.fromhex(org_root.private_hex), recipient_pub, ORG_ROOT_ARMOR_PURPOSE
    )
    return {
        "root_pub": org_root.public_hex,
        "sealed_root_key": record.hex(),
        "owner_kem_pub": recipient_pub,
        "seal_purpose": ORG_ROOT_ARMOR_PURPOSE,
    }


@pytest.fixture
def client(tmp_path, monkeypatch):
    from tools.graph.db import GraphDB
    from tools.dashboard import network_routes

    GraphDB.close_all_pooled()
    orgs = tmp_path / "orgs"
    orgs.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    # The caller's own org, per the scope cascade the route resolves through.
    monkeypatch.setenv("GRAPH_ORG", "acme")
    GraphDB.close_all_pooled()
    GraphDB.create_org_db(
        "personal", type_="personal", path=orgs / "personal.db"
    ).close()
    app = Starlette(routes=[
        Route(
            "/api/network/org-key/sealed",
            network_routes.post_sealed_org_key,
            methods=["POST"],
        ),
    ])
    with TestClient(app) as c:
        yield c
    GraphDB.close_all_pooled()


def _post(client, payload, org=None):
    """Post as the caller's own org (the cascade resolves it) unless named."""
    body = dict(payload)
    if org is not None:
        body["org"] = org
    return client.post("/api/network/org-key/sealed", json=body)


def test_a_sealed_root_is_stored_for_an_unfounded_shell(client):
    org_ops.create_org_shell("acme")
    payload = sealed_payload()
    r = _post(client, payload)
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "root_pub": payload["root_pub"]}

    stored = list(settings_ops.read_owned_set(NETWORK_ORG_KEY_SET_ID, org="acme"))
    assert len(stored) == 1
    assert stored[0].payload == {
        "root_pub": payload["root_pub"],
        "sealed_root_key": payload["sealed_root_key"],
        "owner_kem_pub": payload["owner_kem_pub"],
        "seal_purpose": ORG_ROOT_ARMOR_PURPOSE,
    }


def test_the_stored_seal_actually_opens_with_the_owners_derived_key(client):
    """Round-trip: what was stored must give the org root back to the owner."""
    from tools.network.idkit.sealing import open as seal_open

    org_ops.create_org_shell("acme")
    org_root = KeyPair.generate()
    payload = sealed_payload(org_root)
    assert _post(client, payload).status_code == 200

    stored = list(settings_ops.read_owned_set(NETWORK_ORG_KEY_SET_ID, org="acme"))[0]
    recipient_priv, _ = derive_encapsulation_keypair(
        PERSONAL_SEED, ORG_ROOT_ARMOR_PURPOSE
    )
    recovered = seal_open(
        bytes.fromhex(stored.payload["sealed_root_key"]),
        recipient_priv,
        ORG_ROOT_ARMOR_PURPOSE,
    )
    assert recovered.hex() == org_root.private_hex


def test_resubmitting_the_same_seal_is_a_no_op(client):
    """The seal-then-fold retry path must be able to resubmit."""
    org_ops.create_org_shell("acme")
    payload = sealed_payload()
    assert _post(client, payload).status_code == 200
    assert _post(client, payload).status_code == 200
    assert len(settings_ops.read_owned_set(NETWORK_ORG_KEY_SET_ID, org="acme")) == 1


def test_an_unknown_org_is_404(client):
    """No shell was created, so there is nothing this key could belong to."""
    assert _post(client, sealed_payload()).status_code == 404


def test_sealing_into_another_org_is_refused(client):
    """An org-key blob is offline-attackable; a cross-org write is a real leak."""
    org_ops.create_org_shell("acme")
    r = _post(client, sealed_payload(), org="someone-else")
    assert r.status_code == 403


def test_a_wrong_seal_purpose_is_refused(client):
    org_ops.create_org_shell("acme")
    payload = sealed_payload() | {"seal_purpose": "autonomy/something-else/v1"}
    r = _post(client, payload)
    assert r.status_code == 400
    assert "seal_purpose" in r.json()["error"]


def test_non_hex_sealed_material_is_refused(client):
    org_ops.create_org_shell("acme")
    payload = sealed_payload() | {"sealed_root_key": "not-hex-at-all"}
    assert _post(client, payload).status_code == 400


def test_a_missing_field_is_refused(client):
    org_ops.create_org_shell("acme")
    payload = sealed_payload()
    del payload["owner_kem_pub"]
    r = _post(client, payload)
    assert r.status_code == 400
    assert "owner_kem_pub" in r.json()["error"]


def test_the_key_locks_once_the_ledger_is_founded(client):
    """A founded ledger has committed to this root; it can never be swapped."""
    from tools.network.ledger.store import LedgerStore, org_ledger_db_path
    from tools.network.ledger.found import found_org_ledger

    ref = org_ops.create_org_shell("acme")
    org_root = KeyPair.generate()
    assert _post(client, sealed_payload(org_root)).status_code == 200

    with LedgerStore(org_ledger_db_path("acme")) as store:
        found_org_ledger(
            store,
            org_id=ref.id,
            org_root=org_root,
            personal_root_seed=PERSONAL_SEED,
            now=1_800_000_000_000,
        )

    r = _post(client, sealed_payload())
    assert r.status_code == 409
    assert "locked" in r.json()["error"]
