"""`graph vault seal --org SLUG` must fail LOUDLY, never seal silently personal.

Regression for auto-ha7se: a caller that NAMES an organization it cannot prove
with its bearer (an unscoped/host session) previously had its ``--org`` slug
discarded and its secret written to the operator's own PERSONAL namespace —
reported as ``✓ sealed``. That is a confidentiality mis-landing: the row never
reached the named org, never synced to org members, and org readers never found
it. Both tiers must now refuse it with a non-2xx, and a positively org-scoped
caller must land under its ``<org>:`` prefix rather than the bare personal key.
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


def test_audited_seal_from_unscoped_caller_naming_an_org_is_refused(monkeypatch):
    called = {"write": False}
    monkeypatch.setattr(
        server.graph_ops, "write_by_key",
        lambda *a, **k: called.__setitem__("write", True) or "sid",
    )
    req = _setting_request(
        _audited_body(),
        principal=ApiPrincipal(ApiPrincipalKind.COMPATIBILITY),
        organization="anchore",
    )
    resp = asyncio.run(server.api_graph_setting_create(req))
    assert resp.status_code == 403
    assert b"cannot seal into organization 'anchore'" in resp.body
    # The refusal happens BEFORE any write — nothing lands in personal.
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


def test_secured_seal_from_unscoped_caller_naming_an_org_is_refused(monkeypatch):
    sealed = {"called": False}
    monkeypatch.setattr(
        vault_routes.settings_ops, "write_by_key",
        lambda *a, **k: sealed.__setitem__("called", True) or "sid",
    )
    app = _secured_app(
        monkeypatch,
        principal=ApiPrincipal(ApiPrincipalKind.LOCAL_SESSION, subject="host"),
        organization="anchore",
    )
    with TestClient(app) as client:
        resp = client.post("/api/identity/vault-settings", json={
            "key": "sync-proof",
            "value": "s3cr3t",
            "policy_class_id": "personal-root",
        })
    assert resp.status_code == 403, resp.text
    assert "cannot seal into organization 'anchore'" in resp.json()["error"]
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
        principal=ApiPrincipal(ApiPrincipalKind.LOCAL_SESSION, subject="host"),
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
