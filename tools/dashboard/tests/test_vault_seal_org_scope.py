"""`graph vault seal --org SLUG` lands in that org, never silently personal.

auto-ha7se: a caller naming an organization once had its ``--org`` slug
discarded and its secret written to the operator's own PERSONAL namespace,
reported as ``✓ sealed`` -- a confidentiality mis-landing. auto-kx7uo
(operator ruling 2026-10-01): the operator's host terminal (or dashboard
cookie) seals DIRECTLY into any organization it names, at both tiers, as
``<org>:<name>``; an org session lands under its own ``<org>:``; any other
caller naming an org is refused. In no case does a named org reach the bare
personal key.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.testclient import TestClient

from tools.dashboard import server, vault_routes
from tools.dashboard.api_auth import ApiPrincipal, ApiPrincipalKind


# ── audited tier: POST /api/graph/setting (api_graph_setting_create) ─────────


def _setting_request(body: dict, *, principal, organization) -> Request:
    """A minimal ASGI request carrying a JSON body and a pre-classified
    principal/organization in state, exactly as the identity middleware binds
    them before the handler runs."""
    payload = json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/graph/setting",
        "headers": [],
        "state": {"api_principal": principal, "api_organization": organization},
    }
    return Request(scope, receive=receive)


def _audited_body() -> dict:
    return {
        "set_id": "autonomy.vault.audited",
        "schema_revision": 1,
        "key": "sync-proof",
        "payload": {"value": "s3cr3t"},
    }


def _orgs(monkeypatch, *known):
    from tools.graph import org_ops

    monkeypatch.setattr(org_ops, "get_org",
                        lambda slug, **_k: object() if slug in known else None)
    _sessions(monkeypatch, {"host-1001-122405": "host", "auto-0930-161540": "container"})


def _sessions(monkeypatch, types: dict):
    """The dashboard's launch record: tmux_sessions.type per session."""
    from tools.dashboard.dao import dashboard_db

    monkeypatch.setattr(dashboard_db, "get_session",
                        lambda name: {"type": types[name]} if name in types else None)


#: The operator in person: the dashboard cookie, or a host terminal.
OPERATORS = [
    ApiPrincipal(ApiPrincipalKind.OPERATOR_COOKIE),
    ApiPrincipal(ApiPrincipalKind.LOCAL_SESSION, subject="host-1001-122405"),
]
#: Non-org callers that are NOT the operator's terminal -- refused.
NON_OPERATORS = [
    ApiPrincipal(ApiPrincipalKind.LOCAL_SESSION, subject="auto-0930-161540"),  # personal container
    ApiPrincipal(ApiPrincipalKind.LOCAL_SESSION, subject="unknown-session"),
    ApiPrincipal(ApiPrincipalKind.COMPATIBILITY),
    ApiPrincipal(ApiPrincipalKind.MCP_SERVICE, subject="svc"),
    ApiPrincipal(ApiPrincipalKind.EXTERNAL_SERVICE, subject="svc"),
]


@pytest.mark.parametrize("principal", NON_OPERATORS)
def test_audited_seal_from_a_non_operator_naming_an_org_is_refused(monkeypatch, principal):
    _orgs(monkeypatch, "anchore")
    called = {"write": False}
    monkeypatch.setattr(
        server.graph_ops, "write_by_key",
        lambda *a, **k: called.__setitem__("write", True) or "sid",
    )
    req = _setting_request(_audited_body(), principal=principal, organization="anchore")
    resp = asyncio.run(server.api_graph_setting_create(req))
    assert resp.status_code == 403
    assert b"cannot address organization 'anchore'" in resp.body
    # The refusal happens BEFORE any write — nothing lands in personal.
    assert called["write"] is False


@pytest.mark.parametrize("principal", OPERATORS)
def test_audited_seal_from_the_operator_naming_an_org_lands_in_it(monkeypatch, principal):
    _orgs(monkeypatch, "anchore")
    captured = {}

    def write_by_key(set_id, rev, key, payload, *, org, **kw):
        captured.update(key=key, org=org)
        return "sid"

    monkeypatch.setattr(server.graph_ops, "write_by_key", write_by_key)
    monkeypatch.setattr(server.graph_ops, "take_shadowed_write", lambda *a: None)
    req = _setting_request(_audited_body(), principal=principal, organization="anchore")
    resp = asyncio.run(server.api_graph_setting_create(req))
    assert resp.status_code == 201, resp.body
    # write_by_key derives ``anchore:`` from this org (_apply_org_writeback).
    assert captured == {"key": "sync-proof", "org": "anchore"}


def test_audited_seal_into_an_unknown_org_is_refused(monkeypatch):
    _orgs(monkeypatch, "anchore")
    called = {"write": False}
    monkeypatch.setattr(server.graph_ops, "write_by_key",
                        lambda *a, **k: called.__setitem__("write", True) or "sid")
    req = _setting_request(
        _audited_body(),
        principal=ApiPrincipal(ApiPrincipalKind.LOCAL_SESSION, subject="host-1001-122405"),
        organization="anchroe",
    )
    resp = asyncio.run(server.api_graph_setting_create(req))
    assert resp.status_code == 400
    assert b"no organization named 'anchroe'" in resp.body
    assert called["write"] is False


def test_audited_seal_from_the_matching_org_session_lands_namespaced(monkeypatch):
    captured = {}

    def write_by_key(set_id, rev, key, payload, *, org, **kw):
        captured.update(set_id=set_id, key=key, org=org)
        return "sid"

    monkeypatch.setattr(server.graph_ops, "write_by_key", write_by_key)
    monkeypatch.setattr(server.graph_ops, "take_shadowed_write", lambda *a: None)
    req = _setting_request(
        _audited_body(),
        principal=ApiPrincipal(
            ApiPrincipalKind.ORG_SESSION, subject="auto-writer", org="anchore",
        ),
        organization="anchore",
    )
    resp = asyncio.run(server.api_graph_setting_create(req))
    assert resp.status_code == 201, resp.body
    # The org slug is PRESERVED into write_by_key, which derives the <org>:
    # prefix — it is no longer clobbered to None (the bare personal key).
    assert captured["org"] == "anchore"


# ── secured tier: POST /api/identity/vault-settings (seal_personal_setting) ──


def _secured_app(monkeypatch, *, principal, organization):
    monkeypatch.setattr(
        vault_routes.api_auth, "require_authenticated_api_caller",
        lambda request: None,
    )
    monkeypatch.setattr(
        vault_routes.api_auth, "principal_from_request", lambda request: principal,
    )
    monkeypatch.setattr(
        vault_routes.api_auth, "organization_scope_from_request",
        lambda request: organization,
    )
    return Starlette(routes=vault_routes.ROUTES)


@pytest.mark.parametrize("principal", NON_OPERATORS)
def test_secured_seal_from_a_non_operator_naming_an_org_is_refused(monkeypatch, principal):
    _orgs(monkeypatch, "anchore")
    sealed = {"called": False}
    monkeypatch.setattr(
        vault_routes.settings_ops, "write_by_key",
        lambda *a, **k: sealed.__setitem__("called", True) or "sid",
    )
    app = _secured_app(
        monkeypatch,
        principal=principal,
        organization="anchore",
    )
    with TestClient(app) as client:
        resp = client.post("/api/identity/vault-settings", json={
            "key": "sync-proof",
            "value": "s3cr3t",
            "policy_class_id": "personal-root",
        })
    assert resp.status_code == 403, resp.text
    assert "cannot address organization 'anchore'" in resp.json()["error"]
    assert sealed["called"] is False
    # The plaintext is never echoed into the refusal body.
    assert "s3cr3t" not in resp.text


def test_secured_seal_with_no_named_org_is_not_refused(monkeypatch):
    # The operator's own write (no named org) is untouched by the new guard:
    # it flows past to the ordinary personal-root routing.
    reached = {"routing": False}

    def boom(*_a, **_k):
        reached["routing"] = True
        raise ValueError("routing reached")

    monkeypatch.setattr(vault_routes, "_store", boom)
    app = _secured_app(
        monkeypatch,
        principal=ApiPrincipal(ApiPrincipalKind.LOCAL_SESSION, subject="host-1001-122405"),
        organization=None,
    )
    with TestClient(app) as client:
        resp = client.post("/api/identity/vault-settings", json={
            "key": "gh.token",
            "value": "ghp_x",
            "policy_class_id": "personal-root",
        })
    # Not a 403 refusal — it proceeded into routing (which our stub then trips).
    assert resp.status_code != 403
    assert reached["routing"] is True


@pytest.mark.parametrize("principal", OPERATORS)
def test_secured_seal_from_the_operator_naming_an_org_lands_in_it(monkeypatch, principal):
    _orgs(monkeypatch, "anchore")
    captured = {}

    def write_by_key(set_id, rev, key, payload, *, org, **kw):
        captured.update(key=key, org=org)
        return "sid"

    monkeypatch.setattr(vault_routes.settings_ops, "write_by_key", write_by_key)
    monkeypatch.setattr(vault_routes, "resolve_personal_root_class_id", lambda c: c)
    app = _secured_app(monkeypatch, principal=principal, organization="anchore")
    with TestClient(app) as client:
        resp = client.post("/api/identity/vault-settings", json={
            "key": "sync-proof", "value": "s3cr3t", "policy_class_id": "personal-root",
        })
    assert resp.status_code < 300, resp.text
    assert captured == {"key": "anchore:sync-proof", "org": None}


def test_secured_seal_into_an_unknown_org_is_refused(monkeypatch):
    _orgs(monkeypatch, "anchore")
    sealed = {"called": False}
    monkeypatch.setattr(vault_routes.settings_ops, "write_by_key",
                        lambda *a, **k: sealed.__setitem__("called", True) or "sid")
    app = _secured_app(
        monkeypatch,
        principal=ApiPrincipal(ApiPrincipalKind.LOCAL_SESSION, subject="host-1001-122405"),
        organization="../etc",
    )
    with TestClient(app) as client:
        resp = client.post("/api/identity/vault-settings", json={
            "key": "sync-proof", "value": "s3cr3t", "policy_class_id": "personal-root",
        })
    assert resp.status_code == 400, resp.text
    assert sealed["called"] is False
    assert "s3cr3t" not in resp.text


@pytest.fixture
def real_vault(tmp_path, monkeypatch):
    """A real (cold) personal store with the audited delegate recipient
    published, so both tiers seal for real."""
    from tools.graph import settings_ops
    from tools.graph.db import GraphDB
    from tools.vault import key_holder
    from tools.vault.personal_object import derive_delegate_audited_recipient
    from tools.vault.store import VaultStore

    monkeypatch.setenv("AUTONOMY_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    personal = tmp_path / "personal.db"
    monkeypatch.setattr(key_holder, "_scoped_db", lambda _set_id, _org: personal)
    GraphDB(personal).close()
    GraphDB.close_all_pooled()
    _private, public_hex = derive_delegate_audited_recipient(bytes(range(32)))
    with VaultStore(personal) as store:
        store.put_delegate_audited_recipient(public_hex)
    settings_ops.set_vault_sealer(None)
    settings_ops.set_vault_key_holder(None)
    settings_ops.set_personal_delegate_audited_key(None)
    _orgs(monkeypatch, "anchore")
    yield settings_ops
    GraphDB.close_all_pooled()


def _keys(settings_ops, set_id):
    return {m.key for m in settings_ops.read_set(set_id, org=None, peers=[])}


def test_a_host_audited_seal_lands_as_the_org_row_in_a_real_store(real_vault):
    from tools.graph.schemas.vault_credential import VAULT_AUDITED_SET_ID

    # The real write path (no stub): write_by_key derives the ``anchore:``
    # key from the org the route kept. The secured route derives the key
    # itself, asserted above, before the same personal-store write.
    req = _setting_request(
        _audited_body(),
        principal=ApiPrincipal(ApiPrincipalKind.LOCAL_SESSION, subject="host-1001-122405"),
        organization="anchore",
    )
    resp = asyncio.run(server.api_graph_setting_create(req))
    assert resp.status_code == 201, resp.body
    keys = _keys(real_vault, VAULT_AUDITED_SET_ID)
    assert "anchore:sync-proof" in keys
    assert "sync-proof" not in keys

