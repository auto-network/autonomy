"""Every vault verb that takes a name resolves ``--org SLUG`` from the
operator's terminal to ``SLUG:<name>`` (auto-kx7uo).

The live check found seal working while ``seal --retier`` and ``remove``
answered "no sealed credential named ..." -- the server resolved the bare
name, ignoring the org the host named. One resolver now answers for every
verb (``vault_routes.operator_org_for_request`` -> ``_setting_route``, and the
approval planners' ``context.operator_org``). Each verb is exercised through
its route here; the store is real wherever the verb reads or deletes a row,
and holds a BARE row of the same name so a mis-resolution is visible.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.testclient import TestClient

from tools.dashboard import vault_routes
from tools.dashboard.api_auth import ApiPrincipal, ApiPrincipalKind
from tools.graph.schemas.vault_credential import (
    VAULT_AUDITED_SET_ID,
    VAULT_CREDENTIAL_REVISION,
    VAULT_SECURED_SET_ID,
)

HOST = ApiPrincipal(ApiPrincipalKind.LOCAL_SESSION, subject="host-1001-122405")
CONTAINER = ApiPrincipal(ApiPrincipalKind.LOCAL_SESSION, subject="auto-0930-161540")
NAME = "kx7uo-live-check"


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A real personal store, warm, holding ``NAME`` bare AND ``anchore:NAME``
    at the audited tier, with distinct values."""
    from tools.dashboard.dao import dashboard_db
    from tools.graph import org_ops, settings_ops
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
    private_hex, public_hex = derive_delegate_audited_recipient(bytes(range(32)))
    with VaultStore(personal) as vs:
        vs.put_delegate_audited_recipient(public_hex)
    settings_ops.set_vault_sealer(None)
    settings_ops.set_vault_key_holder(None)
    settings_ops.set_personal_delegate_audited_key(private_hex)
    monkeypatch.setattr(org_ops, "get_org",
                        lambda slug, **_k: object() if slug in ("anchore", "blindhash") else None)
    sessions = {HOST.subject: {"type": "host"}, CONTAINER.subject: {"type": "container"}}
    monkeypatch.setattr(dashboard_db, "get_session", lambda name: sessions.get(name))
    for key, value in ((NAME, "bare-value"), (f"anchore:{NAME}", "anchore-value")):
        settings_ops.write_by_key(VAULT_AUDITED_SET_ID, VAULT_CREDENTIAL_REVISION,
                                  key, {"value": value}, org=None)
    yield settings_ops
    settings_ops.set_personal_delegate_audited_key(None)
    GraphDB.close_all_pooled()


def _keys(settings_ops, set_id=VAULT_AUDITED_SET_ID):
    return {m.key for m in settings_ops.read_set(set_id, org=None, peers=[]).members}


def _vault_app(monkeypatch, principal, organization):
    monkeypatch.setattr(vault_routes.api_auth, "require_authenticated_api_caller",
                        lambda request: None)
    monkeypatch.setattr(vault_routes.api_auth, "principal_from_request",
                        lambda request: principal)
    monkeypatch.setattr(vault_routes.api_auth, "organization_scope_from_request",
                        lambda request: organization)
    return TestClient(Starlette(routes=vault_routes.ROUTES))


# ── remove (and seal --retier, which removes the other tier's row) ─────────

def test_remove_with_org_deletes_the_org_row_and_never_the_bare_one(monkeypatch, store):
    client = _vault_app(monkeypatch, HOST, "anchore")
    r = client.delete(f"/api/vault/credential/{VAULT_AUDITED_SET_ID}/{NAME}")
    assert r.status_code == 200, r.text
    assert r.json()["key"] == f"anchore:{NAME}"
    assert _keys(store) == {NAME}


def test_remove_without_org_deletes_only_the_bare_row(monkeypatch, store):
    client = _vault_app(monkeypatch, HOST, None)
    r = client.delete(f"/api/vault/credential/{VAULT_AUDITED_SET_ID}/{NAME}")
    assert r.status_code == 200, r.text
    assert _keys(store) == {f"anchore:{NAME}"}


def test_remove_naming_an_org_from_a_container_is_refused(monkeypatch, store):
    client = _vault_app(monkeypatch, CONTAINER, "anchore")
    r = client.delete(f"/api/vault/credential/{VAULT_AUDITED_SET_ID}/{NAME}")
    assert r.status_code == 403, r.text
    assert _keys(store) == {NAME, f"anchore:{NAME}"}


def test_retier_finds_and_removes_the_org_row(monkeypatch, store):
    """`graph vault seal NAME --org anchore --tier secured --retier` over an
    audited anchore row: the CLI finds anchore:NAME (exactly, not the bare
    row) and the remove route deletes that row."""
    from tools.graph import vault_cmd

    client = _vault_app(monkeypatch, HOST, "anchore")
    removed = []

    class Client:
        def read_set(self, set_id, *, org):
            assert org == "anchore"
            return SimpleNamespace(members=[
                m for m in store.read_set(set_id, org=None, peers=[]).members
            ])

        def remove_vault_credential(self, set_id, name, *, org):
            r = client.delete(f"/api/vault/credential/{set_id}/{name}")
            assert r.status_code == 200, r.text
            removed.append(r.json()["key"])

    tier, member = vault_cmd._find_member(Client(), NAME, "anchore")
    assert (tier, member.key) == ("audited", f"anchore:{NAME}")
    Client().remove_vault_credential(VAULT_AUDITED_SET_ID, NAME, org="anchore")
    assert removed == [f"anchore:{NAME}"]
    assert _keys(store) == {NAME}


# ── read, audited (the deliver route) ──────────────────────────────────────

def test_audited_read_with_org_delivers_the_org_row(monkeypatch, store):
    from tools.dashboard import vault_release_delivery

    delivered = []

    def deliver_payload(row, payload):
        delivered.append((row["request"]["setting"]["key"], payload["value"]))
        return {"path": "/run/secrets/x"}

    monkeypatch.setattr(vault_release_delivery, "deliver_payload", deliver_payload)
    client = _vault_app(monkeypatch, HOST, "anchore")
    r = client.post(f"/api/vault/credential/{VAULT_AUDITED_SET_ID}/{NAME}/deliver", json={})
    assert r.status_code == 200, r.text
    assert delivered == [(f"anchore:{NAME}", "anchore-value")]


# ── share (the source row) ─────────────────────────────────────────────────

def test_share_with_org_shares_the_org_row(monkeypatch, store):
    written = []
    real_write = store.write_by_key

    def write_by_key(set_id, rev, key, payload, *, org, **kw):
        written.append((key, payload["value"], org))
        return real_write(set_id, rev, key, payload, org=None, **kw)

    monkeypatch.setattr(vault_routes.settings_ops, "write_by_key", write_by_key)
    client = _vault_app(monkeypatch, HOST, "anchore")
    r = client.post(f"/api/vault/credential/{VAULT_AUDITED_SET_ID}/{NAME}/share",
                    json={"to_org": "blindhash"})
    assert r.status_code == 200, r.text
    assert r.json()["from_key"] == f"anchore:{NAME}"
    assert written == [(NAME, "anchore-value", "blindhash")]


# ── list ───────────────────────────────────────────────────────────────────

def test_list_with_org_shows_only_that_orgs_rows(monkeypatch, store):
    from tools.dashboard import server
    from tools.graph import ops as graph_ops

    monkeypatch.setattr(server.api_auth, "require_authenticated_api_caller", lambda r: None)
    scope = {
        "type": "http", "method": "GET", "headers": [], "query_string": b"",
        "path": f"/api/graph/settings/{VAULT_AUDITED_SET_ID}",
        "path_params": {"set_id": VAULT_AUDITED_SET_ID},
        "state": {"api_principal": HOST, "api_organization": "anchore"},
    }
    token = graph_ops.set_caller_org("anchore")
    try:
        resp = asyncio.run(server.api_graph_settings_list(Request(scope)))
    finally:
        graph_ops.reset_caller_org(token)
    assert resp.status_code == 200, resp.body
    assert {m["key"] for m in json.loads(resp.body)["members"]} == {f"anchore:{NAME}"}


# ── read, secured (vault_open) and request (vault_seal): approvals ─────────

def _approvals(monkeypatch, principal, organization):
    from tools.dashboard import approvals_routes

    created = []

    class Bridge:
        def claims_kind(self, kind):
            return True

        def create(self, kind, p, request, **kw):
            created.append((kind, kw.get("operator_org")))
            return "approval-1"

    monkeypatch.setattr(approvals_routes, "_approval_http_bridge", lambda: Bridge())
    monkeypatch.setattr(approvals_routes.api_auth, "principal_from_request",
                        lambda request: principal)
    monkeypatch.setattr(approvals_routes.api_auth, "organization_scope_from_request",
                        lambda request: organization)
    return TestClient(Starlette(routes=list(approvals_routes.ROUTES))), created


@pytest.mark.parametrize("kind", ["vault_open", "vault_seal"])
def test_vault_approvals_carry_the_operators_org(monkeypatch, store, kind):
    client, created = _approvals(monkeypatch, HOST, "anchore")
    r = client.post("/api/approvals", json={"kind": kind, "request": {"key": NAME}})
    assert r.status_code == 200, r.text
    assert created == [(kind, "anchore")]


@pytest.mark.parametrize("kind", ["vault_open", "vault_seal"])
def test_vault_approvals_naming_an_org_from_a_container_are_refused(monkeypatch, store, kind):
    client, created = _approvals(monkeypatch, CONTAINER, "anchore")
    r = client.post("/api/approvals", json={"kind": kind, "request": {"key": NAME}})
    assert r.status_code == 403, r.text
    assert created == []


def test_other_approval_kinds_ignore_the_header(monkeypatch, store):
    client, created = _approvals(monkeypatch, CONTAINER, "anchore")
    r = client.post("/api/approvals", json={"kind": "commit_sign", "request": {"x": 1}})
    assert r.status_code == 200, r.text
    assert created == [("commit_sign", None)]


def test_secured_read_resolves_the_org_row(monkeypatch, store):
    """vault_open's planner hands the operator's org to freeze_request, whose
    _setting_route resolves anchore:NAME in the operator's store."""
    from tools.dashboard import vault_open_approvals, vault_open_central
    from tools.dashboard.approval_kind_registry import ApprovalPlanningContext

    assert vault_open_approvals._setting_route(
        HOST, VAULT_SECURED_SET_ID, NAME, operator_org="anchore",
    ) == (f"anchore:{NAME}", None)
    seen = []

    def freeze(principal, body, **kw):
        seen.append(kw.get("operator_org"))
        raise ValueError("stop after freeze")

    # The requester proof is mailbox_central's, exercised by its own tests.
    monkeypatch.setattr(vault_open_central, "_requesting_session", lambda c: HOST.subject)
    plan = vault_open_central.build_request_planner(freeze=freeze)
    context = ApprovalPlanningContext(
        approval_id="a", planning_time=0.0,
        requester_ref={"kind": "session", "id": HOST.subject, "label": HOST.subject},
        application_scope="x", requester_principal_kind=HOST.kind.value,
        operator_org="anchore",
    )
    with pytest.raises(ValueError, match="stop after freeze"):
        plan(context, {"set_id": VAULT_SECURED_SET_ID, "key": NAME})
    assert seen == ["anchore"]


def test_request_deposit_lands_in_the_org_row(store):
    from tools.dashboard import vault_seal_central
    from tools.dashboard.approval_kind_registry import ApprovalPlanningContext

    context = ApprovalPlanningContext(
        approval_id="a", planning_time=0.0, requester_ref={}, application_scope="x",
        requester_principal_kind=HOST.kind.value, operator_org="anchore",
    )
    assert vault_seal_central.routed_key(context, VAULT_SECURED_SET_ID, NAME) == f"anchore:{NAME}"
    bare = ApprovalPlanningContext(
        approval_id="a", planning_time=0.0, requester_ref={}, application_scope="x",
        requester_principal_kind=HOST.kind.value,
    )
    assert vault_seal_central.routed_key(bare, VAULT_SECURED_SET_ID, NAME) == NAME
