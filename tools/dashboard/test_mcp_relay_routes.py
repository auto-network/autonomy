"""Integration tests for the relay-facing /api/mcp/* routes.

Exercises the route logic (service-token auth, pending-approval creation, approve/
decline reconciliation, crosstalk gating) via Starlette's TestClient against a
temp DB and a stubbed approval store — no full dashboard needed.
"""

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard import mcp_relay_routes as routes
from tools.dashboard.dao import mcp_relay_db as db

TOKEN = "test-service-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class _FakeApprovals:
    """Minimal stand-in for the approval_requests DAO."""
    def __init__(self):
        self.rows = {}
        self._n = 0

    def create(self, *, kind, session, request, staged, created_at):
        self._n += 1
        rid = f"appr-{self._n}"
        self.rows[rid] = {"id": rid, "kind": kind, "session": session,
                          "request": request, "result": None}
        return rid

    def get(self, rid):
        return self.rows.get(rid)

    def decide(self, rid, result):
        self.rows[rid]["result"] = result


class _FakeBus:
    def __init__(self):
        self.events = []

    async def broadcast(self, topic, payload):
        self.events.append((topic, payload))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "mcp_relay.db")
    monkeypatch.setenv(routes.SERVICE_TOKEN_ENV, TOKEN)
    fake_ar = _FakeApprovals()
    fake_bus = _FakeBus()
    monkeypatch.setattr(routes, "ar", fake_ar)
    monkeypatch.setattr(routes, "event_bus", fake_bus)
    app = Starlette(routes=routes.ROUTES)
    c = TestClient(app)
    c.ar = fake_ar  # expose for tests to simulate operator decisions
    return c


def test_requires_service_token(client):
    r = client.post("/api/mcp/session/resolve", json={"openai_session": "v1/s"})
    assert r.status_code == 401


def test_missing_token_env_is_fail_closed(client, monkeypatch):
    monkeypatch.delenv(routes.SERVICE_TOKEN_ENV, raising=False)
    r = client.post("/api/mcp/session/resolve", headers=AUTH,
                    json={"openai_session": "v1/s"})
    assert r.status_code == 503


def test_new_session_goes_pending_and_opens_one_approval(client):
    body = {"openai_session": "v1/chatA", "openai_subject": "v1/subj",
            "openai_org": "v1/oorg", "intent": "tunnel help", "requested_org": "autonomy"}
    r = client.post("/api/mcp/session/resolve", headers=AUTH, json=body)
    assert r.status_code == 200 and r.json()["status"] == "pending"
    assert len(client.ar.rows) == 1  # one approval opened
    appr = next(iter(client.ar.rows.values()))
    assert appr["kind"] == "mcp_peer_link"
    assert appr["request"]["intent"] == "tunnel help"
    # polling again must NOT spawn a second popup while the first is undecided
    client.post("/api/mcp/session/resolve", headers=AUTH, json=body)
    assert len(client.ar.rows) == 1


def test_approved_binding_resolves_with_org_and_level(client):
    body = {"openai_session": "v1/chatB", "requested_org": "autonomy"}
    client.post("/api/mcp/session/resolve", headers=AUTH, json=body)
    rid = next(iter(client.ar.rows))
    # operator approves in the popup -> executor writes the binding
    client.ar.decide(rid, {"approved": True})
    db.approve_session("v1/chatB", autonomy_org="autonomy", level="readwrite",
                       expires_at=None)
    r = client.post("/api/mcp/session/resolve", headers=AUTH, json=body)
    j = r.json()
    assert j["status"] == "approved"
    assert j["autonomy_org"] == "autonomy" and j["level"] == "readwrite"


def test_declined_approval_becomes_denied(client):
    body = {"openai_session": "v1/chatC"}
    client.post("/api/mcp/session/resolve", headers=AUTH, json=body)
    rid = next(iter(client.ar.rows))
    client.ar.decide(rid, {"approved": False})  # operator declines
    r = client.post("/api/mcp/session/resolve", headers=AUTH, json=body)
    assert r.json()["status"] == "denied"


def test_crosstalk_requires_linked_peer_then_grants(client):
    # not linked yet -> peer_not_linked
    r = client.post("/api/mcp/crosstalk/resolve", headers=AUTH,
                    json={"openai_session": "v1/chatD", "target_session": "auto-x"})
    assert r.json()["status"] == "peer_not_linked"
    # link the peer
    db.upsert_pending_session("v1/chatD")
    db.approve_session("v1/chatD", autonomy_org="autonomy", level="read", expires_at=None)
    # now crosstalk resolve -> pending + opens a crosstalk approval
    r = client.post("/api/mcp/crosstalk/resolve", headers=AUTH,
                    json={"openai_session": "v1/chatD", "target_session": "auto-x",
                          "target_org": "personal"})
    assert r.json()["status"] == "pending"
    xrid = [rid for rid, a in client.ar.rows.items() if a["kind"] == "mcp_crosstalk"][0]
    # operator approves the crosstalk grant
    client.ar.decide(xrid, {"approved": True})
    db.approve_crosstalk("v1/chatD", "auto-x", expires_at=None)
    r = client.post("/api/mcp/crosstalk/resolve", headers=AUTH,
                    json={"openai_session": "v1/chatD", "target_session": "auto-x"})
    assert r.json()["status"] == "approved"
