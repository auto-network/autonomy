"""The vault credential remove seam derives the namespace like seal/vault_open."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard import vault_routes
from tools.dashboard.api_auth import ApiPrincipal, ApiPrincipalKind

SECURED = "autonomy.vault.secured"
AUDITED = "autonomy.vault.audited"


def _client(monkeypatch, principal, *, rows=None, removed=None):
    monkeypatch.setattr(
        vault_routes.api_auth,
        "require_authenticated_api_caller",
        lambda request: None,
    )
    monkeypatch.setattr(
        vault_routes.api_auth, "principal_from_request", lambda request: principal,
    )
    rows = rows or {}

    def layers_for(set_id, key, *, org):
        row = rows.get((set_id, key))
        return {
            "base": {"id": row} if row else None,
            "shadowed_bases": rows.get("shadowed") or [],
        }

    monkeypatch.setattr(vault_routes.settings_ops, "layers_for", layers_for)
    monkeypatch.setattr(
        vault_routes.settings_ops,
        "remove_setting",
        lambda sid, *, org: (removed if removed is not None else []).append(
            (sid, org)
        ),
    )
    return TestClient(Starlette(routes=vault_routes.ROUTES))


def _org_session(org="autonomy"):
    return ApiPrincipal(ApiPrincipalKind.ORG_SESSION, subject="sess-1", org=org)


def test_org_session_removes_its_own_derived_key(monkeypatch):
    removed = []
    client = _client(
        monkeypatch,
        _org_session(),
        rows={(SECURED, "autonomy:pg.test"): "sid-1"},
        removed=removed,
    )
    response = client.delete(f"/api/vault/credential/{SECURED}/pg.test")
    assert response.status_code == 200, response.text
    assert response.json()["key"] == "autonomy:pg.test"
    assert removed == [("sid-1", None)]


def test_prefixed_name_gets_the_derives_the_namespace_refusal(monkeypatch):
    removed = []
    client = _client(
        monkeypatch,
        _org_session(),
        rows={(SECURED, "autonomy:pg.test"): "sid-1"},
        removed=removed,
    )
    response = client.delete(f"/api/vault/credential/{SECURED}/autonomy%3Apg.test")
    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert "unprefixed credential name" in error
    assert "derives the organization namespace" in error
    assert removed == []


def test_org_session_on_the_audited_set_is_refused_cleanly(monkeypatch):
    """No org-keyed namespace is declared there (until auto-rhorp A-1)."""
    client = _client(monkeypatch, _org_session())
    response = client.delete(f"/api/vault/credential/{AUDITED}/pg.test")
    assert response.status_code == 403, response.text
    assert "namespace" in response.json()["error"]


def test_personal_caller_removes_a_bare_key(monkeypatch):
    removed = []
    client = _client(
        monkeypatch,
        _org_session(org="personal"),
        rows={(SECURED, "pg.test"): "sid-2"},
        removed=removed,
    )
    response = client.delete(f"/api/vault/credential/{SECURED}/pg.test")
    assert response.status_code == 200, response.text
    assert removed == [("sid-2", "personal")]


def test_unknown_name_is_a_404_scoped_to_the_caller_namespace(monkeypatch):
    client = _client(monkeypatch, _org_session())
    response = client.delete(f"/api/vault/credential/{SECURED}/absent.key")
    assert response.status_code == 404, response.text
    assert "absent.key" in response.json()["error"]


def test_non_vault_set_is_refused(monkeypatch):
    client = _client(monkeypatch, _org_session())
    response = client.delete("/api/vault/credential/dashboard.feature_flags/x")
    assert response.status_code == 400, response.text
    assert "not a vault credential set" in response.json()["error"]


def test_ambiguous_live_rows_refuse_removal_by_name(monkeypatch):
    client = _client(
        monkeypatch,
        _org_session(),
        rows={
            (SECURED, "autonomy:pg.test"): "sid-1",
            "shadowed": [{"id": "sid-old"}],
        },
    )
    response = client.delete(f"/api/vault/credential/{SECURED}/pg.test")
    assert response.status_code == 409, response.text
    assert "more than one live row" in response.json()["error"]
