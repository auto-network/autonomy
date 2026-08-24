from starlette.testclient import TestClient
from starlette.applications import Starlette

from tools.dashboard import vault_routes
from tools.vault.factors import create_password_factor
from tools.vault.store import VaultStore


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
