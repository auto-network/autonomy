"""auto-26e8a declared default: organization-shared inference accounts are
for members' launches. Through the dashboard's settings routes, a caller that
is not the operator in person reads them with every secret part redacted;
writes are open to agents (operator ruling 2026-10-02). The launcher opens them in-process, which is not a
route (tests: agents/tests/test_org_shared_account_launch.py)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.testclient import TestClient

from tools.dashboard import restricted_sets, unlock_routes
from tools.dashboard.tests.test_remote_api_authority import OPERATOR, Principal, _org
from tools.graph.schemas.vault_credential import ORG_HARNESS_ACCOUNTS_SET_ID as SET

ROWS = [{"id": "r1", "set_id": SET, "key": "claude.account.O1.access",
         "payload": {"value": "at-SECRET"}},
        {"id": "r2", "set_id": SET, "key": "claude.account.O1.alias",
         "payload": {"value": "team"}}]
OTHER = {"id": "r3", "set_id": "autonomy.workspace", "key": "dev", "payload": {"value": "x"}}


def _settings_routes():
    from tools.dashboard import server

    wanted = ("/api/graph/settings/{set_id}", "/api/graph/setting",
              "/api/graph/setting/{id}", "/api/graph/setting/{id}/override")
    return [r for r in server.routes if getattr(r, "path", None) in wanted]


@pytest.fixture
def client(monkeypatch):
    from tools.graph import ops as graph_ops

    monkeypatch.setattr(unlock_routes, "gate_enforced", lambda: True)
    monkeypatch.setattr(graph_ops, "read_set", lambda set_id, **_k: SimpleNamespace(
        as_payload=lambda: {"members": [r for r in ROWS + [OTHER] if r["set_id"] == set_id]}))
    writes = []
    monkeypatch.setattr(graph_ops, "add_setting",
                        lambda *a, **k: writes.append(("add", a[0])) or "new-id")
    monkeypatch.setattr(graph_ops, "override_setting",
                        lambda target, *a, **k: writes.append(("override", target)) or "o-id")
    monkeypatch.setattr(graph_ops, "remove_setting",
                        lambda target, **k: writes.append(("remove", target)))
    monkeypatch.setattr(graph_ops, "get_setting", lambda sid, **_k: SimpleNamespace(
        set_id=SET if sid in ("r1", "r2") else "autonomy.workspace",
        to_dict=lambda: next(r for r in ROWS + [OTHER] if r["id"] == sid)))
    app = Starlette(routes=_settings_routes(), middleware=[Middleware(Principal)])
    with TestClient(app) as c:
        c.writes = writes
        yield c
    Principal.principal = None


def _as(principal):
    Principal.principal = principal


def test_a_session_reads_shared_accounts_with_secrets_redacted(client):
    _as(_org("acme"))
    members = {m["key"]: m["payload"]["value"]
               for m in client.get(f"/api/graph/settings/{SET}").json()["members"]}
    assert members == {"claude.account.O1.access": "[redacted]",
                       "claude.account.O1.alias": "team"}
    assert client.get("/api/graph/setting/r1").json()["payload"]["value"] == "[redacted]"
    # Other sets are untouched.
    assert client.get("/api/graph/settings/autonomy.workspace").json()["members"][0][
        "payload"] == {"value": "x"}


def test_the_operator_in_person_reads_them_in_full(client):
    _as(OPERATOR)
    members = client.get(f"/api/graph/settings/{SET}").json()["members"]
    assert {m["payload"]["value"] for m in members} == {"at-SECRET", "team"}


@pytest.mark.parametrize("method, path, body, expected", [
    ("POST", "/api/graph/setting", {"set_id": SET, "schema_revision": 1,
                                    "key": "claude.account.O1.access", "payload": {"value": "x"}},
     None),
    ("POST", "/api/graph/setting/r1/override", {"payload": {"value": "x"}}, ("override", "r1")),
    ("DELETE", "/api/graph/setting/r1", None, ("remove", "r1")),
])
def test_a_session_may_add_replace_or_remove_a_shared_account(client, method, path, body,
                                                                expected):
    """Operator ruling 2026-10-02: agents may write the set."""
    _as(_org("acme"))
    response = client.request(method, path, json=body)
    assert response.status_code != 403 and "operator-only-set" not in response.text
    if expected is not None:     # creating a vaulted row needs a warm vault here
        assert client.writes == [expected]


def test_redaction_keeps_account_naming_parts():
    out = restricted_sets.redact({"members": ROWS})
    assert [m["payload"]["value"] for m in out["members"]] == ["[redacted]", "team"]
