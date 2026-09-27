"""Shared gate exercised against real token, session and Settings stores."""

import hashlib
from dataclasses import FrozenInstanceError, asdict
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from tools.dashboard import capability_gate as gate
from tools.dashboard.dao import auth_db, dashboard_db
from tools.graph import settings_ops
from tools.graph.db import GraphDB
from tools.graph.schemas.workspace_capability_enable import SET_ID, SCHEMA_REVISION

TOKEN = "capability-gate-test-token"
SESSION = "auto-gate-test"
WORKSPACE = "gate-workspace"
ORG = "gate-org"
HEADER = f"Bearer {TOKEN}"


@pytest.fixture
def env(tmp_path, monkeypatch):
    from agents import workspace_settings

    GraphDB.close_all_pooled()
    orgs = tmp_path / "orgs"
    orgs.mkdir()
    GraphDB(orgs / f"{ORG}.db").close()
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    auth_db.init_db(tmp_path / "auth.db")
    dashboard_db.init_db(tmp_path / "dashboard.db")

    def workspace(project):
        if project != WORKSPACE:
            raise KeyError(project)
        return SimpleNamespace(id=WORKSPACE, graph_project=ORG)

    monkeypatch.setattr(workspace_settings, "get_workspace", workspace)
    monkeypatch.setattr(workspace_settings, "invalidate_caches", lambda: None)
    auth_db.insert_token(hashlib.sha256(TOKEN.encode()).hexdigest(), SESSION, ORG)
    dashboard_db.upsert_session(SESSION, "agent", WORKSPACE)
    yield
    GraphDB.close_all_pooled()


def grant(enabled=True, capability="browser"):
    settings_ops.upsert_by_key(
        SET_ID, SCHEMA_REVISION, f"{WORKSPACE}:{capability}",
        {"contract": capability, "enabled": enabled}, org=ORG)


@pytest.mark.parametrize("header", [None, "", "Basic token", "Bearer ", "Bearer unknown"])
def test_missing_malformed_unknown_token(env, header):
    with pytest.raises(gate.CapabilityRefused) as exc:
        gate.resolve_caller(header)
    assert exc.value.status == 401
    assert exc.value.detail == str(exc.value)


@pytest.mark.parametrize("project", ["", "unknown-workspace"])
def test_unmapped_workspace(env, project):
    # Session upserts preserve the launcher's original project. Create a
    # distinct launch record instead of trying to rewrite an existing one.
    token = "unmapped-session-token"
    session = "auto-unmapped"
    auth_db.insert_token(hashlib.sha256(token.encode()).hexdigest(), session, ORG)
    dashboard_db.upsert_session(session, "agent", project)
    with pytest.raises(gate.CapabilityRefused) as exc:
        gate.resolve_caller(f"Bearer {token}")
    assert exc.value.status == 403


def test_resolution_does_not_require_a_grant(env):
    scope = gate.resolve_caller(HEADER)
    assert scope == gate.CallerScope(org=ORG, workspace=WORKSPACE, session=SESSION)
    with pytest.raises(FrozenInstanceError):
        scope.workspace = "victim"
    with pytest.raises(gate.CapabilityRefused) as exc:
        gate.require_capability(HEADER, "browser")
    assert exc.value.status == 403


def test_disable_effective_on_next_call(env):
    grant()
    assert gate.require_capability(HEADER, "browser").workspace == WORKSPACE
    grant(False)
    with pytest.raises(gate.CapabilityRefused) as exc:
        gate.require_capability(HEADER, "browser")
    assert exc.value.status == 403


def test_token_revocation_effective_on_next_call(env):
    grant()
    gate.require_capability(HEADER, "browser")
    auth_db.revoke_token(SESSION)
    with pytest.raises(gate.CapabilityRefused) as exc:
        gate.require_capability(HEADER, "browser")
    assert exc.value.status == 401


def test_grant_does_not_enable_another_capability(env):
    grant(capability="issue_tracker")
    with pytest.raises(gate.CapabilityRefused) as exc:
        gate.require_capability(HEADER, "browser")
    assert exc.value.status == 403


def test_request_body_cannot_supply_scope(env):
    grant()

    async def endpoint(request):
        return JSONResponse(asdict(gate.require_capability(
            request.headers.get("authorization"), "browser")))

    client = TestClient(Starlette(routes=[Route("/gate", endpoint, methods=["POST"])]))
    response = client.post("/gate", headers={"Authorization": HEADER}, json={
        "org": "victim-org", "workspace": "victim-workspace", "session": "victim-session"})
    assert response.status_code == 200
    assert response.json() == {"org": ORG, "workspace": WORKSPACE, "session": SESSION}
