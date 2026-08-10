"""The REPL's fail-closed caller ladder, against real auth/dashboard DBs.

Every rung refuses on its own evidence: no bearer, unknown token, revoked
token, session without a workspace, unknown workspace, workspace without
the repl_login grant, grant explicitly disabled. Only the full chain —
launcher-stamped token, launcher-stamped project, live capability grant —
produces a caller.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.connectors import repl_auth
from tools.dashboard.dao import auth_db, dashboard_db
from tools.graph import settings_ops
from tools.graph.schemas.workspace_capability_enable import (
    SET_ID as ENABLE_SET_ID,
    SCHEMA_REVISION as ENABLE_REVISION,
)

ORG = "acme"
WORKSPACE = "finance-ws"
SESSION = "auto-0810-000001"
TOKEN = "raw-crosstalk-token-for-tests"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@pytest.fixture
def env(tmp_path, monkeypatch):
    from tools.graph.db import GraphDB

    GraphDB.close_all_pooled()
    orgs_dir = tmp_path / "orgs"
    orgs_dir.mkdir()
    GraphDB(orgs_dir / f"{ORG}.db").close()
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    auth_db.init_db(tmp_path / "auth.db")
    dashboard_db.init_db(tmp_path / "dashboard.db")

    import agents.workspace_settings as workspace_settings

    def fake_get_workspace(workspace_id):
        if workspace_id != WORKSPACE:
            raise KeyError(f"unknown workspace: {workspace_id!r}")
        return SimpleNamespace(id=WORKSPACE, graph_project=ORG)

    monkeypatch.setattr(workspace_settings, "get_workspace", fake_get_workspace)
    monkeypatch.setattr(workspace_settings, "invalidate_caches", lambda: None)
    monkeypatch.setattr(repl_auth, "_workspace_map_refreshed_at", 0.0)
    yield tmp_path
    GraphDB.close_all_pooled()


def _stamp_session(project: str = WORKSPACE,
                   session: str = SESSION, token: str = TOKEN) -> None:
    auth_db.insert_token(_hash(token), session)
    dashboard_db.upsert_session(session, "agent", project)


def _grant(enabled: bool = True) -> None:
    settings_ops.upsert_by_key(
        ENABLE_SET_ID, ENABLE_REVISION, f"{WORKSPACE}:repl_login",
        {"contract": "repl_login", "enabled": enabled}, org=ORG)


def _auth(root: Path | None, header: str | None):
    return repl_auth.authenticate(autonomy_root=root, authorization=header)


def _refused(root, header, *, status: int, needle: str) -> None:
    with pytest.raises(repl_auth.ReplAuthError) as excinfo:
        _auth(root, header)
    assert excinfo.value.status == status
    assert needle in str(excinfo.value)


def test_missing_or_malformed_bearer_is_401(env):
    for header in (None, "", "Bearer ", "Basic abc", TOKEN):
        _refused(env, header, status=401, needle="missing bearer token")


def test_unknown_token_is_401(env):
    _refused(env, "Bearer nope", status=401, needle="invalid or revoked token")


def test_revoked_token_is_401(env):
    _stamp_session()
    _grant()
    assert _auth(env, f"Bearer {TOKEN}").session == SESSION
    auth_db.revoke_token(SESSION)
    _refused(env, f"Bearer {TOKEN}", status=401,
             needle="invalid or revoked token")


def test_session_without_row_or_project_is_403(env):
    auth_db.insert_token(_hash(TOKEN), SESSION)  # token but no session row
    _refused(env, f"Bearer {TOKEN}", status=403,
             needle="does not map to a workspace")
    dashboard_db.upsert_session(SESSION, "agent", "")
    _refused(env, f"Bearer {TOKEN}", status=403,
             needle="does not map to a workspace")


def test_unknown_workspace_is_403(env):
    _stamp_session(project="never-heard-of-it")
    _refused(env, f"Bearer {TOKEN}", status=403, needle="unknown workspace")


def test_workspace_without_grant_is_403(env):
    _stamp_session()
    _refused(env, f"Bearer {TOKEN}", status=403,
             needle="does not enable repl_login")


def test_disabled_grant_is_403(env):
    _stamp_session()
    _grant(enabled=False)
    _refused(env, f"Bearer {TOKEN}", status=403,
             needle="does not enable repl_login")


def test_grant_revocation_takes_effect_without_restart(env):
    _stamp_session()
    _grant()
    assert _auth(env, f"Bearer {TOKEN}").workspace_id == WORKSPACE
    _grant(enabled=False)  # the enable set is read fresh per request
    _refused(env, f"Bearer {TOKEN}", status=403,
             needle="does not enable repl_login")


def test_full_chain_yields_caller(env):
    _stamp_session()
    _grant()
    caller = _auth(env, f"Bearer {TOKEN}")
    assert caller == repl_auth.ReplCaller(
        session=SESSION, workspace_id=WORKSPACE, org=ORG)


def test_unconfigured_root_is_503(env):
    _refused(None, f"Bearer {TOKEN}", status=503, needle="--autonomy-root")
