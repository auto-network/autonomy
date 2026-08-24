import subprocess
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import JSONResponse

from tools.dashboard import vault_routes
from tools.dashboard.api_auth import ApiPrincipal, ApiPrincipalKind
from tools.vault.factors import create_password_factor
from tools.network.idkit.keys import KeyPair
from tools.vault.root_anchor import create_root_anchor
from tools.vault.policy_class import create_root_reachable_class
from tools.vault.store import VaultStore


DASHBOARD = Path(__file__).resolve().parents[1]


def test_vault_browser_modules_parse_as_javascript():
    for relative in (
        "static/js/ceremony/root-anchor.js",
        "static/js/ceremony/vault-unlock.js",
        "static/js/ceremony/open-vault.js",
        "static/js/pages/worktrees.js",
    ):
        result = subprocess.run(
            ["node", "--check", str(DASHBOARD / relative)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr


def test_password_material_route_never_accepts_plaintext(monkeypatch, tmp_path):
    path = tmp_path / "vault.db"
    monkeypatch.setattr(vault_routes, "_store", lambda: VaultStore(path))
    monkeypatch.setattr(vault_routes, "_guard", lambda request: None)
    app = Starlette(routes=vault_routes.ROUTES)
    factor = create_password_factor("correct horse", factor_id="pw")
    with TestClient(app) as client:
        response = client.post("/api/identity/factors/password", json={
            "factor_id": factor.published.factor_id,
            "public_key": factor.published.public_key,
            "armor": factor.armor,
            "password": "must-not-be-accepted",
        })
    assert response.status_code == 400
    assert "plaintext" in response.json()["error"]


def test_password_material_route_stores_canonical_material(monkeypatch, tmp_path):
    path = tmp_path / "vault.db"
    monkeypatch.setattr(vault_routes, "_store", lambda: VaultStore(path))
    monkeypatch.setattr(vault_routes, "_guard", lambda request: None)
    app = Starlette(routes=vault_routes.ROUTES)
    factor = create_password_factor("correct horse", factor_id="pw")
    with TestClient(app) as client:
        response = client.post("/api/identity/factors/password", json={
            "factor_id": factor.published.factor_id,
            "public_key": factor.published.public_key,
            "armor": factor.armor,
        })
    assert response.status_code == 201
    with VaultStore(path) as store:
        assert store.get_published_factor("pw") == factor.published


def test_password_material_route_rejects_malformed_armor(monkeypatch, tmp_path):
    path = tmp_path / "vault.db"
    monkeypatch.setattr(vault_routes, "_store", lambda: VaultStore(path))
    monkeypatch.setattr(vault_routes, "_guard", lambda request: None)
    app = Starlette(routes=vault_routes.ROUTES)
    with TestClient(app) as client:
        response = client.post("/api/identity/factors/password", json={
            "factor_id": "pw",
            "public_key": "0" * 64,
            "armor": "not armor",
        })
    assert response.status_code == 400
    assert "armor" in response.json()["error"]


def test_legacy_open_route_never_returns_a_content_key(monkeypatch, tmp_path):
    path = tmp_path / "vault.db"
    monkeypatch.setattr(vault_routes, "_store", lambda: VaultStore(path))
    monkeypatch.setattr(vault_routes, "_guard", lambda request: None)
    app = Starlette(routes=vault_routes.ROUTES)
    with TestClient(app) as client:
        response = client.post(
            "/api/identity/settings/demo/open",
            json={"openers": {"pw": "00" * 32}},
        )
    assert response.status_code == 410
    assert "cek" not in response.text.lower()
    assert "content-encryption keys are never returned" in response.json()["error"]


def test_root_signed_anchor_enrolls_then_mints_a_root_reachable_class(
    monkeypatch, tmp_path,
):
    path = tmp_path / "vault.db"
    root = KeyPair.generate()
    anchor, _seed = create_root_anchor(
        root,
        anchor_id="personal-default",
        display_name="Personal root vault access",
        created_at="2026-08-24T00:00:00Z",
    )
    monkeypatch.setattr(vault_routes, "_store", lambda: VaultStore(path))
    monkeypatch.setattr(vault_routes, "_guard", lambda request: None)
    monkeypatch.setattr(vault_routes, "_personal_root_pub", lambda: root.public_hex)
    app = Starlette(routes=vault_routes.ROUTES)

    with TestClient(app) as client:
        enrolled = client.post(
            "/api/identity/vault-anchors", json={"anchor": anchor.to_dict()},
        )
        assert enrolled.status_code == 201, enrolled.text
        minted = client.post(
            f"/api/identity/vault-anchors/{anchor.anchor_id}/classes",
            json={"display_name": "Personal root vault"},
        )
        assert minted.status_code == 201, minted.text
        minted_again = client.post(
            f"/api/identity/vault-anchors/{anchor.anchor_id}/classes",
            json={"display_name": "Personal root vault"},
        )
        assert minted_again.status_code == 200, minted_again.text
        inventory = client.get("/api/identity/vault-anchors")

    assert inventory.status_code == 200
    body = inventory.json()
    assert body["anchors"] == [anchor.to_dict()]
    assert len(body["classes"]) == 1
    policy_class = body["classes"][0]
    assert policy_class["governance"] == {
        "v": 1,
        "form": "root-reachable",
        "anchor_id": anchor.anchor_id,
        "display_name": "Personal root vault",
    }
    assert policy_class["generations"][0]["wraps"][0]["factor_type"] == (
        "personal-root-anchor"
    )


def test_anchor_from_another_root_is_refused_before_persistence(monkeypatch, tmp_path):
    path = tmp_path / "vault.db"
    root = KeyPair.generate()
    foreign, _seed = create_root_anchor(
        KeyPair.generate(),
        anchor_id="foreign",
        display_name="Foreign root",
        created_at="2026-08-24T00:00:00Z",
    )
    monkeypatch.setattr(vault_routes, "_store", lambda: VaultStore(path))
    monkeypatch.setattr(vault_routes, "_guard", lambda request: None)
    monkeypatch.setattr(vault_routes, "_personal_root_pub", lambda: root.public_hex)
    app = Starlette(routes=vault_routes.ROUTES)

    with TestClient(app) as client:
        response = client.post(
            "/api/identity/vault-anchors", json={"anchor": foreign.to_dict()},
        )

    assert response.status_code == 400
    assert "different personal root" in response.json()["error"]
    with VaultStore(path) as store:
        assert store.root_anchor_ids() == []


def test_personal_setting_route_pins_write_to_personal_and_never_echoes_value(
    monkeypatch, tmp_path,
):
    path = tmp_path / "vault.db"
    root = KeyPair.generate()
    anchor, _seed = create_root_anchor(
        root,
        anchor_id="personal-default",
        display_name="Personal root vault",
        created_at="2026-08-24T00:00:00Z",
    )
    policy_class = create_root_reachable_class(
        anchor.published_recipient(),
        display_name="Personal root vault",
        created_at="2026-08-24T00:01:00Z",
    )
    with VaultStore(path) as store:
        store.put_root_anchor(anchor)
        store.put_class(policy_class)
    captured = {}

    def write_by_key(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return "setting-1"

    monkeypatch.setattr(vault_routes, "_store", lambda: VaultStore(path))
    monkeypatch.setattr(
        vault_routes.api_auth, "require_authenticated_api_caller", lambda request: None,
    )
    monkeypatch.setattr(
        vault_routes,
        "_guard",
        lambda request: (_ for _ in ()).throw(
            AssertionError("the personal write seam must not require operator authority")
        ),
    )
    monkeypatch.setattr(
        vault_routes.api_auth,
        "principal_from_request",
        lambda request: ApiPrincipal(
            ApiPrincipalKind.ORG_SESSION, subject="personal-writer", org="personal",
        ),
    )
    monkeypatch.setattr(vault_routes, "_personal_root_pub", lambda: root.public_hex)
    monkeypatch.setattr(vault_routes.settings_ops, "write_by_key", write_by_key)
    app = Starlette(routes=vault_routes.ROUTES)
    secret = "-----BEGIN OPENSSH PRIVATE KEY-----\ndisposable\n"
    with TestClient(app) as client:
        response = client.post("/api/identity/vault-settings", json={
            "key": "mac.ssh.disposable-proof",
            "value": secret,
            "policy_class_id": policy_class.class_id,
        })

    assert response.status_code == 201, response.text
    assert secret not in response.text
    assert captured["args"][:3] == (
        "autonomy.vault.secured", 1, "mac.ssh.disposable-proof",
    )
    assert captured["args"][3] == {"value": secret}
    assert captured["kwargs"]["org"] is None
    assert captured["kwargs"]["vault_policy_class_id"] == policy_class.class_id


def test_org_bearer_is_confined_to_its_derived_credential_namespace(
    monkeypatch, tmp_path,
):
    path = tmp_path / "vault.db"
    root = KeyPair.generate()
    anchor, _seed = create_root_anchor(
        root,
        anchor_id="personal-default",
        display_name="Personal root vault",
        created_at="2026-08-24T00:00:00Z",
    )
    policy_class = create_root_reachable_class(
        anchor.published_recipient(),
        display_name="Personal root vault",
        created_at="2026-08-24T00:01:00Z",
    )
    with VaultStore(path) as store:
        store.put_root_anchor(anchor)
        store.put_class(policy_class)
    captured = {}

    monkeypatch.setattr(
        vault_routes.api_auth,
        "require_authenticated_api_caller",
        lambda request: None,
    )
    monkeypatch.setattr(
        vault_routes.api_auth,
        "principal_from_request",
        lambda request: ApiPrincipal(
            ApiPrincipalKind.ORG_SESSION,
            subject="auto-writer",
            org="autonomy",
        ),
    )
    monkeypatch.setattr(vault_routes, "_store", lambda: VaultStore(path))
    monkeypatch.setattr(vault_routes, "_personal_root_pub", lambda: root.public_hex)
    monkeypatch.setattr(
        vault_routes.settings_ops,
        "write_by_key",
        lambda *args, **kwargs: (
            captured.update({"args": args, "kwargs": kwargs}) or "setting-1"
        ),
    )
    app = Starlette(routes=vault_routes.ROUTES)
    with TestClient(app) as client:
        response = client.post("/api/identity/vault-settings", json={
            "key": "mac.ssh",
            "value": "fake-private-key",
            "policy_class_id": "personal-root",
        })
    assert response.status_code == 201, response.text
    assert response.json()["key"] == "autonomy:mac.ssh"
    assert response.json()["policy_class_id"] == policy_class.class_id
    assert captured["args"][2] == "autonomy:mac.ssh"
    assert captured["args"][3] == {"value": "fake-private-key"}
    assert captured["kwargs"]["org"] is None


@pytest.mark.parametrize("spoofed", [
    "other-org:mac.ssh",
    "autonomy:mac.ssh",
    ":mac.ssh",
    "../mac.ssh",
    "mac.ssh\nforged-log-entry",
])
def test_org_bearer_cannot_inject_or_spoof_a_namespace(
    monkeypatch, spoofed,
):
    monkeypatch.setattr(
        vault_routes.api_auth,
        "require_authenticated_api_caller",
        lambda request: None,
    )
    monkeypatch.setattr(
        vault_routes.api_auth,
        "principal_from_request",
        lambda request: ApiPrincipal(
            ApiPrincipalKind.ORG_SESSION,
            subject="auto-writer",
            org="autonomy",
        ),
    )
    app = Starlette(routes=vault_routes.ROUTES)
    with TestClient(app) as client:
        response = client.post("/api/identity/vault-settings", json={
            "key": spoofed,
            "value": "fake-private-key",
            "policy_class_id": "not-reached",
        })
    assert response.status_code == 400
    assert "unprefixed credential name" in response.json()["error"]


def test_org_bearer_is_refused_when_the_schema_did_not_opt_in(monkeypatch):
    monkeypatch.setattr(
        vault_routes.api_auth,
        "require_authenticated_api_caller",
        lambda request: None,
    )
    monkeypatch.setattr(
        vault_routes.api_auth,
        "principal_from_request",
        lambda request: ApiPrincipal(
            ApiPrincipalKind.ORG_SESSION,
            subject="auto-writer",
            org="autonomy",
        ),
    )
    monkeypatch.setattr(
        vault_routes.schema_registry,
        "declared_org_writeback_key_strategy",
        lambda *_args: None,
    )
    app = Starlette(routes=vault_routes.ROUTES)
    with TestClient(app) as client:
        response = client.post("/api/identity/vault-settings", json={
            "key": "mac.ssh",
            "value": "fake-private-key",
            "policy_class_id": "not-reached",
        })
    assert response.status_code == 403
    assert "does not declare" in response.json()["error"]


def test_personal_setting_route_requires_an_attributable_caller(monkeypatch):
    monkeypatch.setattr(
        vault_routes.api_auth,
        "require_authenticated_api_caller",
        lambda request: JSONResponse({"error": "authentication required"}, status_code=401),
    )
    monkeypatch.setattr(
        vault_routes.settings_ops,
        "write_by_key",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("an unattributed request must not reach the write seam")
        ),
    )
    app = Starlette(routes=vault_routes.ROUTES)
    with TestClient(app) as client:
        response = client.post("/api/identity/vault-settings", json={
            "key": "mac.ssh",
            "value": "fake-private-key",
            "policy_class_id": "class-1",
        })
    assert response.status_code == 401


def test_personal_setting_route_failure_never_echoes_value(monkeypatch, tmp_path):
    path = tmp_path / "vault.db"
    root = KeyPair.generate()
    anchor, _seed = create_root_anchor(
        root,
        anchor_id="personal-default",
        display_name="Personal root vault",
        created_at="2026-08-24T00:00:00Z",
    )
    policy_class = create_root_reachable_class(
        anchor.published_recipient(),
        display_name="Personal root vault",
        created_at="2026-08-24T00:01:00Z",
    )
    with VaultStore(path) as store:
        store.put_root_anchor(anchor)
        store.put_class(policy_class)
    monkeypatch.setattr(vault_routes, "_store", lambda: VaultStore(path))
    monkeypatch.setattr(
        vault_routes.api_auth,
        "require_authenticated_api_caller",
        lambda request: None,
    )
    monkeypatch.setattr(
        vault_routes.api_auth,
        "principal_from_request",
        lambda request: ApiPrincipal(
            ApiPrincipalKind.ORG_SESSION,
            subject="auto-writer",
            org="autonomy",
        ),
    )
    monkeypatch.setattr(vault_routes, "_personal_root_pub", lambda: root.public_hex)
    monkeypatch.setattr(
        vault_routes.settings_ops,
        "write_by_key",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            vault_routes.settings_ops.VaultSealerMissing("vault is cold")
        ),
    )
    app = Starlette(routes=vault_routes.ROUTES)
    secret = "-----BEGIN OPENSSH PRIVATE KEY-----\nnever-echo-me\n"
    with TestClient(app) as client:
        response = client.post("/api/identity/vault-settings", json={
            "key": "mac.ssh.disposable-proof",
            "value": secret,
            "policy_class_id": policy_class.class_id,
        })
    assert response.status_code == 423
    assert response.json() == {"error": "vault is cold"}
    assert secret not in response.text
