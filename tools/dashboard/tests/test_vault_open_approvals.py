"""End-to-end secured Setting release through the approval rendezvous."""

from __future__ import annotations

import hashlib
import json
import secrets

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard import api_auth, approvals_routes, vault_open_approvals
from tools.dashboard.dao import approval_requests as ar
from tools.graph import ops, settings_ops
from tools.graph.schemas.vault_credential import (
    VAULT_CREDENTIAL_REVISION,
    VAULT_SECURED_SET_ID,
)
from tools.graph.tests.vault_read_harness import VaultWorld, clear_seams
from tools.vault.policy_class import extend_class, revoke_factor
from tools.vault.store import VaultStore
from tools.vault.testkit import make_test_identity


@pytest.fixture
def vault_open_env(tmp_path, monkeypatch):
    graph_db = tmp_path / "personal.db"
    monkeypatch.setenv("GRAPH_DB", str(graph_db))
    monkeypatch.delenv("GRAPH_API", raising=False)
    # ``GRAPH_DB`` is a whole-store pin and the real credential schema is
    # explicitly personal-homed. Make that declared home resolve to the pin,
    # rather than weakening the production conflict guard for this harness.
    from tools.graph import db as graph_db_module
    original_org_db_path = graph_db_module._org_db_path
    monkeypatch.setattr(
        graph_db_module,
        "_org_db_path",
        lambda org, root=None: (
            graph_db if org == "personal"
            else original_org_db_path(org, root)
        ),
    )
    monkeypatch.setattr(ar, "DB_PATH", tmp_path / "approvals.db")
    monkeypatch.setattr(
        vault_open_approvals.dashboard_db,
        "get_session",
        lambda name: {
            "tmux_name": name,
            "project": "autonomy-codex",
            "label": "Vault test requester",
        } if name == "auto-real" else None,
    )

    agent = api_auth.ApiPrincipal(
        api_auth.ApiPrincipalKind.ORG_SESSION,
        subject="auto-real",
        org="autonomy",
    )
    operator = api_auth.ApiPrincipal(
        api_auth.ApiPrincipalKind.OPERATOR_COOKIE,
        subject="operator-session",
    )

    def principal(request):
        if request.headers.get("x-test-stranger") == "1":
            return api_auth.ApiPrincipal(
                api_auth.ApiPrincipalKind.ORG_SESSION,
                subject="auto-stranger",
                org="autonomy",
            )
        return operator if request.headers.get("x-test-operator") == "1" else agent

    monkeypatch.setattr(api_auth, "principal_from_request", principal)

    async def no_push(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(approvals_routes.web_push, "register_approval_pending", no_push)
    monkeypatch.setattr(approvals_routes.web_push, "cancel_approval", lambda *_a, **_k: None)
    approvals_routes._executing.clear()
    approvals_routes._decision_waiters.clear()
    vault_open_approvals.clear_ephemeral_deliveries()

    world = VaultWorld(tmp_path / "vault").register()
    try:
        yield graph_db, world, TestClient(Starlette(routes=approvals_routes.ROUTES))
    finally:
        world.close()
        clear_seams()
        approvals_routes._executing.clear()
        approvals_routes._decision_waiters.clear()
        vault_open_approvals.clear_ephemeral_deliveries()


def test_random_secret_is_delivered_byte_identical_without_opener_leak(
    vault_open_env,
):
    """The cp-1 harness: generate, seal, approve, deliver, compare digest."""
    graph_db, world, client = vault_open_env
    secret = secrets.token_urlsafe(48)
    expected_digest = hashlib.sha256(secret.encode()).hexdigest()
    setting_id = ops.add_setting(
        VAULT_SECURED_SET_ID,
        VAULT_CREDENTIAL_REVISION,
        "test.disposable",
        {"value": secret},
        org=ops.CALLER_ORG,
    )
    assert world.identity is not None
    with VaultStore(graph_db) as store:
        store.put_class(world.policy_class)
        store.put_password_factor(
            world.identity.factor_id,
            world.identity.published.public_key,
            world.identity.armor,
        )

    with client:
        created = client.post("/api/approvals", json={
            "kind": "vault_open",
            # A malicious body cannot redirect attribution away from the bearer.
            "session": "auto-spoofed",
            "request": {
                "set_id": VAULT_SECURED_SET_ID,
                "key": "test.disposable",
                "ttl_seconds": 60,
            },
        })
        assert created.status_code == 200, created.text
        rid = created.json()["id"]
        row = ar.get(rid)
        assert row["session"] == "auto-real"
        assert row["request"]["requester"] == {
            "session": "auto-real",
            "workspace": "autonomy-codex",
            "label": "Vault test requester",
        }
        assert row["request"]["setting"]["id"] == setting_id
        assert secret not in json.dumps(row)

        refused = client.get(f"/api/approvals/{rid}")
        assert refused.status_code == 403
        ceremony = client.get(
            f"/api/approvals/{rid}", headers={"x-test-operator": "1"},
        )
        assert ceremony.status_code == 200, ceremony.text
        bootstrap = ceremony.json()
        assert bootstrap["ceremony"]["policy"] == "password"
        assert bootstrap["ceremony"]["factors"][0]["armor"] == world.identity.armor
        assert "ceremony" not in client.get(
            f"/api/approvals/{rid}?wait=0"
        ).json()
        assert client.get(
            f"/api/approvals/{rid}?wait=0",
            headers={"x-test-stranger": "1"},
        ).status_code == 403

        # Neither the requesting bearer nor a malformed decline can smuggle
        # opener material into the durable approval result.
        assert client.post(
            f"/api/approvals/{rid}/decision",
            json={"approved": True, "openers": {"pw": "00" * 32}},
        ).status_code == 401
        assert client.post(
            f"/api/approvals/{rid}/decision",
            headers={"x-test-operator": "1"},
            json={"approved": False, "openers": {"pw": "00" * 32}},
        ).status_code == 401
        assert ar.get(rid)["result"] is None

        opener_hex = world.opener_seeds[world.identity.factor_id].hex()
        decided = client.post(
            f"/api/approvals/{rid}/decision",
            headers={"x-test-operator": "1"},
            json={
                "approved": True,
                "openers": {world.identity.factor_id: opener_hex},
            },
        )
        assert decided.status_code == 200, decided.text
        delivered = client.get(f"/api/approvals/{rid}?wait=10").json()
        replay = client.get(f"/api/approvals/{rid}?wait=0").json()

    assert delivered["result"] == {
        "approved": True,
        "execution": {"ok": True, "value": {"value": secret}},
    }
    assert replay["result"]["execution"]["ok"] is False
    assert "value" not in replay["result"]["execution"]
    assert hashlib.sha256(
        delivered["result"]["execution"]["value"]["value"].encode()
    ).hexdigest() == expected_digest
    persisted = ar.get(rid)
    assert "openers" not in json.dumps(persisted)
    assert opener_hex not in json.dumps(persisted)
    assert secret not in json.dumps(persisted)
    assert persisted["result"]["execution"] == {
        "ok": True,
        "delivery": "ephemeral-single-use",
    }
    assert "cek" not in json.dumps(delivered).lower()


def test_vault_open_refuses_setting_drift(vault_open_env):
    graph_db, world, client = vault_open_env
    setting_id = ops.add_setting(
        VAULT_SECURED_SET_ID,
        VAULT_CREDENTIAL_REVISION,
        "test.drift",
        {"value": "first"},
        org=ops.CALLER_ORG,
    )
    with VaultStore(graph_db) as store:
        store.put_class(world.policy_class)
        store.put_password_factor(
            world.identity.factor_id,
            world.identity.published.public_key,
            world.identity.armor,
        )
    with client:
        rid = client.post("/api/approvals", json={
            "kind": "vault_open",
            "request": {
                "set_id": VAULT_SECURED_SET_ID,
                "key": "test.drift",
            },
        }).json()["id"]
        settings_ops.override_setting(setting_id, {"value": "second"}, org=None)
        client.post(
            f"/api/approvals/{rid}/decision",
            headers={"x-test-operator": "1"},
            json={
                "approved": True,
                "openers": {
                    world.identity.factor_id:
                        world.opener_seeds[world.identity.factor_id].hex(),
                },
            },
        )
        result = client.get(f"/api/approvals/{rid}?wait=10").json()["result"]
    assert result["execution"]["ok"] is False
    assert "changed before approval" in result["execution"]["error"]
    assert "value" not in result["execution"]


def test_vault_open_uses_the_generation_named_by_an_older_setting(vault_open_env):
    """A later revocation must not substitute today's wraps for old ciphertext."""
    graph_db, world, client = vault_open_env
    ops.add_setting(
        VAULT_SECURED_SET_ID,
        VAULT_CREDENTIAL_REVISION,
        "test.older-generation",
        {"value": "sealed-before-revocation"},
        org=ops.CALLER_ORG,
    )
    original = world.policy_class
    old_generation_id = original.current().gen_id
    survivor = make_test_identity()
    extended = extend_class(original, world.opener_seeds, survivor.published)
    current = revoke_factor(
        extended, world.identity.factor_id, created_at="2026-08-24T00:00:00Z",
    )
    assert current.current().gen_id != old_generation_id
    with VaultStore(graph_db) as store:
        store.put_class(current)
        store.put_password_factor(
            world.identity.factor_id,
            world.identity.published.public_key,
            world.identity.armor,
        )
        store.put_password_factor(
            survivor.factor_id,
            survivor.published.public_key,
            survivor.armor,
        )

    with client:
        created = client.post("/api/approvals", json={
            "kind": "vault_open",
            "request": {
                "set_id": VAULT_SECURED_SET_ID,
                "key": "test.older-generation",
            },
        })
        assert created.status_code == 200, created.text
        rid = created.json()["id"]
        staged = ar.get(rid)["staged"]
        assert staged["class_snapshot"]["generation"]["gen_id"] == old_generation_id
        review = client.get(
            f"/api/approvals/{rid}", headers={"x-test-operator": "1"},
        ).json()

    assert {factor["factor_id"] for factor in review["ceremony"]["factors"]} == {
        world.identity.factor_id,
        survivor.factor_id,
    }
