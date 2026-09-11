"""Sign-in preparation must isolate failures, not turn one bad input into a
lockout of every sign-in method.

``signon_preparation.collect()`` is awaited by the browser BEFORE the
credential POST on the password, passkey AND recovery paths. If any single
input raises — one organization's ledger, the fleet runtime read, the
personal recovery store — ``get_preparation`` answers 503 and the operator
cannot sign in at all, including with the recovery code. Before fa760a61 the
unlock plan isolated failures per organization and a failed fleet read was
reported as unreadable and skipped.

Each test here stubs every OTHER input healthy and breaks exactly one.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from tools.dashboard import (
    fleet_enrollment_routes as fleet,
    identity_routes,
    link_serving_supervisor,
    network_routes,
    org_storage_delegate,
    signon_preparation,
    unlock_routes,
    vault_routes,
)


def _entry(slug: str) -> dict:
    return {
        "slug": slug,
        "genesis_id": "ab" * 32,
        "org_uuid": "8a2d6c7a-498c-42ba-a4a6-b3b27a024bac",
        "committed_membership_org": True,
        "checkpoint": {"needed": False},
    }


@pytest.fixture
def healthy(monkeypatch):
    """Every preparation input answers; tests then break exactly one."""
    monkeypatch.setattr(identity_routes, "_personal_member",
                        lambda: SimpleNamespace(payload={"root_pub": "cd" * 32}))
    monkeypatch.setattr(unlock_routes, "personal_vault_recovery",
                        lambda: JSONResponse({"recovery": "ok"}))
    monkeypatch.setattr(vault_routes, "root_anchor_inventory", lambda: {"anchors": []})
    monkeypatch.setattr(fleet, "runtime_preparation",
                        lambda: JSONResponse({"enabled": False}))
    monkeypatch.setattr(fleet, "_local_completion_state", lambda: (None, None, None))
    monkeypatch.setattr(link_serving_supervisor, "serve_cert_state", lambda org: {})
    monkeypatch.setattr(network_routes, "_first_member", lambda set_id, org: None)
    monkeypatch.setattr(org_storage_delegate, "prepare",
                        lambda org: {"key_exists": False, "parents": []})
    monkeypatch.setattr(signon_preparation, "organization_plans",
                        lambda: iter([(_entry("good"), "ef" * 32), (_entry("bad"), "ef" * 32)]))
    monkeypatch.setattr(signon_preparation, "organization_encryption_recovery",
                        lambda org: {"genesis_id": "ab" * 32, "counter": 0, "credentials": []})


def _prepared_slugs(result: dict) -> list[str]:
    return [o["slug"] for o in result["organizations"] if not o.get("error")]


def test_one_organizations_failure_does_not_block_sign_in(healthy, monkeypatch):
    """One corrupt or half-migrated org ledger must cost THAT org its
    preparation, not the operator every sign-in method."""
    def prepare(org):
        if org == "bad":
            raise RuntimeError("ledger has no genesis event")
        return {"key_exists": False, "parents": []}

    monkeypatch.setattr(org_storage_delegate, "prepare", prepare)

    result = signon_preparation.collect()

    assert result["vault"]["recovery"] == "ok"
    assert _prepared_slugs(result) == ["good"]
    bad = [o for o in result["organizations"] if o["slug"] == "bad"]
    # Reported as unavailable, or omitted — never raised.
    assert not bad or bad[0].get("error")


def test_encryption_preparation_failure_preserves_signing_and_other_org_inputs(healthy, monkeypatch):
    def prepare(org):
        if org == "bad":
            raise ValueError("cannot read credential records")
        return {"genesis_id": "ab" * 32, "counter": 0, "credentials": []}
    monkeypatch.setattr(signon_preparation, "organization_encryption_recovery", prepare)
    result = signon_preparation.collect()
    assert _prepared_slugs(result) == ["good", "bad"]
    assert result["organizations"][0]["encryption_recovery"]["counter"] == 0
    assert result["organizations"][1]["encryption_recovery"]["error"]
    assert "storage_delegate" in result["organizations"][1]


def test_fleet_runtime_failure_does_not_block_sign_in(healthy, monkeypatch):
    """A stale roster row or a connector that is down is a fleet problem;
    the personal root still opens and every org still prepares."""
    monkeypatch.setattr(fleet, "runtime_preparation",
                        lambda: JSONResponse({"error": "roster mismatch"}, status_code=400))

    result = signon_preparation.collect()

    assert result["vault"]["recovery"] == "ok"
    assert _prepared_slugs(result) == ["good", "bad"]
    assert not result["runtime"] or result["runtime"].get("error")


def test_personal_recovery_store_failure_does_not_block_access_sign_in(healthy, monkeypatch):
    """The personal recovery inventory feeds the vault phase, not the access
    unlock. When it cannot be read (two genesis rows, a locked store), the
    access sign-in must still be prepared."""
    monkeypatch.setattr(unlock_routes, "personal_vault_recovery",
                        lambda: JSONResponse({"error": "multiple genesis rows"}, status_code=503))

    result = signon_preparation.collect()

    assert _prepared_slugs(result) == ["good", "bad"]
    assert not result["vault"] or result["vault"].get("error")


def test_route_answers_sealed_preparation_when_one_org_fails(healthy, monkeypatch):
    """End to end: the GET must be 200 with ciphertext, not 503, when one
    organization's preparation raises."""
    import json
    from tools.vault.personal_object import derive_delegate_audited_recipient
    from tools.network.idkit import sealing
    private, public = derive_delegate_audited_recipient(b"r" * 32)
    class _Store:
        def __init__(self, *_a, **_k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get_delegate_audited_recipient(self):
            return public

    monkeypatch.setattr(signon_preparation, "VaultStore", _Store)
    monkeypatch.setattr(signon_preparation, "_scoped_db", lambda *_a, **_k: ":memory:")

    def prepare(org):
        if org == "bad":
            raise RuntimeError("ledger has no genesis event")
        return {"key_exists": False, "parents": []}

    monkeypatch.setattr(org_storage_delegate, "prepare", prepare)
    app = Starlette(routes=[
        Route("/api/identity/unlock/preparation", signon_preparation.get_preparation),
    ])
    with TestClient(app) as client:
        response = client.get("/api/identity/unlock/preparation")
    assert response.status_code == 200, response.text
    assert set(response.json()) == {"sealed"}
    decrypted = json.loads(sealing.open(bytes.fromhex(response.json()["sealed"]),
                                       private, signon_preparation.PURPOSE))
    assert _prepared_slugs(decrypted) == ["good"]
    assert decrypted["organizations"][1]["error"]


def test_plan_failure_does_not_abort_iteration_before_healthy_org(monkeypatch):
    from tools.graph import org_ops
    monkeypatch.setattr(org_ops, "list_orgs", lambda: [
        SimpleNamespace(slug="bad"), SimpleNamespace(slug="good")])
    monkeypatch.setattr(org_ops, "persona_pub_for_org", lambda genesis: "ef" * 32)
    def plan(slug, *_args):
        if slug == "bad":
            raise RuntimeError("ledger read failed")
        return _entry(slug)
    monkeypatch.setattr(network_routes, "_org_unlock_plan", plan)
    plans = list(signon_preparation.organization_plans())
    assert plans[0] == ({"slug": "bad", "error": "organization-preparation-unavailable"}, None)
    assert plans[1][0] == _entry("good")


def test_checkpoint_failure_does_not_block_another_org(healthy, monkeypatch):
    from tools.dashboard import membership_checkpoint
    bad = _entry("bad")
    bad["checkpoint"]["needed"] = True
    monkeypatch.setattr(signon_preparation, "organization_plans", lambda: iter([
        (bad, "ef" * 32), (_entry("good"), "ef" * 32)]))
    def fail(*_args, **_kwargs):
        raise RuntimeError("checkpoint read failed")
    monkeypatch.setattr(membership_checkpoint, "checkpoint_due", fail)
    result = signon_preparation.collect()
    assert _prepared_slugs(result) == ["good"]
    assert result["organizations"][0]["error"]


@pytest.mark.parametrize("owner,name,key", [
    (fleet, "_local_completion_state", "completion"),
    (link_serving_supervisor, "serve_cert_state", "personal_serve"),
    (vault_routes, "root_anchor_inventory", "vault"),
])
def test_other_unavailable_inputs_remain_explicit_failures(healthy, monkeypatch, owner, name, key):
    def fail(*_args):
        raise RuntimeError("input read failed")
    monkeypatch.setattr(owner, name, fail)
    result = signon_preparation.collect()
    assert result[key]["error"]
    assert _prepared_slugs(result) == ["good", "bad"]
