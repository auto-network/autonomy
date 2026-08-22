"""Integration tests for the relay-facing /api/mcp/* routes.

Exercises the route logic (service-token auth, pending-approval creation, approve/
decline reconciliation, crosstalk gating) via Starlette's TestClient against a
temp DB and a stubbed approval store — no full dashboard needed.
"""

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard import mcp_peer_approvals as kinds
from tools.dashboard import mcp_relay_routes as routes
from tools.dashboard.dao import auth_db
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
    auth_db.init_db(tmp_path / "auth.db")  # crosstalk_messages store for relay/collect
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
            "openai_org": "v1/oorg", "intent": "tunnel help",
            "requested_org": "autonomy", "requested_level": "readwrite"}
    r = client.post("/api/mcp/session/resolve", headers=AUTH, json=body)
    assert r.status_code == 200 and r.json()["status"] == "pending"
    assert len(client.ar.rows) == 1  # one approval opened
    appr = next(iter(client.ar.rows.values()))
    assert appr["kind"] == "mcp_peer_link"
    assert appr["request"]["intent"] == "tunnel help"
    assert appr["request"]["requested_org"] == "autonomy"
    assert appr["request"]["requested_level"] == "readwrite"
    # polling again must NOT spawn a second popup while the first is undecided
    client.post("/api/mcp/session/resolve", headers=AUTH, json=body)
    assert len(client.ar.rows) == 1


def test_approval_uses_a_minted_handle_not_the_raw_session(client):
    client.post("/api/mcp/session/resolve", headers=AUTH,
                json={"openai_session": "v1/secretbearersession", "intent": "help"})
    appr = next(iter(client.ar.rows.values()))
    assert appr["session"] != "v1/secretbearersession"          # not the bearer-equiv session
    assert "secretbearersession" not in appr["session"]         # no leak of the raw session
    assert appr["session"].startswith("ChatGPT-")               # the minted human-readable id
    assert appr["request"]["handle"] == appr["session"]
    assert appr["request"]["openai_session"] == "v1/secretbearersession"  # raw kept for executor


def test_enrich_link_attaches_org_list_and_configured_default(monkeypatch):
    from tools.dashboard import mcp_peer_approvals as kinds
    from tools.graph.schemas import dashboard_shell
    monkeypatch.setattr(dashboard_shell, "shell_default_org", lambda: "autonomy")
    assert "mcp_peer_link" in kinds.ENRICH
    out = kinds.ENRICH["mcp_peer_link"]({"request": {}})
    assert "orgs" in out and isinstance(out["orgs"], list)  # dropdown source (empty in test env)
    assert out["default_org"] == "autonomy"


def test_session_status_is_read_only_never_pops(client):
    body = {"openai_session": "v1/chatB", "intent": "x"}
    client.post("/api/mcp/session/resolve", headers=AUTH, json=body)
    assert len(client.ar.rows) == 1
    # per-request status check must not open approvals
    for _ in range(3):
        r = client.post("/api/mcp/session/status", headers=AUTH,
                        json={"openai_session": "v1/chatB"})
        assert r.json()["status"] == "pending"
    assert len(client.ar.rows) == 1


def test_approved_binding_resolves_with_org_and_level(client):
    body = {"openai_session": "v1/chatC", "intent": "x"}
    client.post("/api/mcp/session/resolve", headers=AUTH, json=body)
    rid = next(iter(client.ar.rows))
    client.ar.decide(rid, {"approved": True})
    db.approve_session("v1/chatC", autonomy_org="autonomy", level="readwrite",
                       expires_at=None)
    # per-request status now reflects the live binding
    r = client.post("/api/mcp/session/status", headers=AUTH,
                    json={"openai_session": "v1/chatC"})
    j = r.json()
    assert j["status"] == "approved"
    assert j["autonomy_org"] == "autonomy" and j["level"] == "readwrite"

    # A read/write grant already satisfies a later read-only request; asking for
    # less authority must not renew the TTL or open another approval.
    r = client.post("/api/mcp/session/resolve", headers=AUTH,
                    json={"openai_session": "v1/chatC", "intent": "read now",
                          "requested_org": "autonomy", "requested_level": "read"})
    assert r.json()["status"] == "approved"
    assert len(client.ar.rows) == 1


def test_rehello_reuses_a_live_binding_until_scope_changes(client):
    body = {"openai_session": "v1/chatR", "intent": "read please"}
    client.post("/api/mcp/session/resolve", headers=AUTH, json=body)
    rid = next(iter(client.ar.rows))
    client.ar.decide(rid, {"approved": True})
    db.approve_session("v1/chatR", autonomy_org="autonomy", level="read", expires_at=None)
    # Repeating hello without a scope change is a status check, not a fresh popup.
    r = client.post("/api/mcp/session/resolve", headers=AUTH,
                    json={"openai_session": "v1/chatR", "intent": "read again",
                          "requested_org": "autonomy", "requested_level": "read"})
    assert r.json()["status"] == "approved"  # existing access preserved
    assert len([a for a in client.ar.rows.values() if a["kind"] == "mcp_peer_link"]) == 1

    # A real privilege upgrade opens one new approval while preserving the
    # existing read grant until the operator decides it.
    r = client.post("/api/mcp/session/resolve", headers=AUTH,
                    json={"openai_session": "v1/chatR", "intent": "now I need write",
                          "requested_org": "autonomy", "requested_level": "readwrite"})
    assert r.json()["status"] == "approved"
    assert r.json()["request_status"] == "pending"
    assert len([a for a in client.ar.rows.values() if a["kind"] == "mcp_peer_link"]) == 2

    # Polling the same upgrade while its approval is open remains deduplicated.
    client.post("/api/mcp/session/resolve", headers=AUTH,
                json={"openai_session": "v1/chatR", "intent": "now I need write",
                      "requested_org": "autonomy", "requested_level": "readwrite"})
    assert len([a for a in client.ar.rows.values() if a["kind"] == "mcp_peer_link"]) == 2


def test_invalid_requested_level_is_rejected_before_opening_approval(client):
    r = client.post("/api/mcp/session/resolve", headers=AUTH,
                    json={"openai_session": "v1/chat-invalid", "intent": "help",
                          "requested_level": "admin"})
    assert r.status_code == 400
    assert not client.ar.rows


def test_declined_approval_becomes_denied(client):
    body = {"openai_session": "v1/chatD", "intent": "x"}
    client.post("/api/mcp/session/resolve", headers=AUTH, json=body)
    rid = next(iter(client.ar.rows))
    client.ar.decide(rid, {"approved": False})  # operator declines
    # the per-request status check reflects the decline as denied (blocks tools)
    r = client.post("/api/mcp/session/status", headers=AUTH,
                    json={"openai_session": "v1/chatD"})
    assert r.json()["status"] == "denied"
    # ...but a re-hello is still allowed to request again (pops a fresh popup)
    r = client.post("/api/mcp/session/resolve", headers=AUTH, json=body)
    assert r.json()["status"] == "pending"


def test_crosstalk_requires_linked_peer_then_carries_message_and_grants(client):
    # not linked yet -> peer_not_linked (linking is a separate step, never a send)
    r = client.post("/api/mcp/crosstalk/resolve", headers=AUTH,
                    json={"openai_session": "v1/chatX", "target_session": "auto-x",
                          "message": "hi"})
    assert r.json()["status"] == "peer_not_linked"
    # link the peer
    db.upsert_pending_session("v1/chatX")
    db.approve_session("v1/chatX", autonomy_org="autonomy", level="read", expires_at=None)
    # crosstalk resolve -> pending, and the approval CARRIES THE MESSAGE (that's what
    # the operator authorizes) + returns the approval id for the relay to hold on.
    r = client.post("/api/mcp/crosstalk/resolve", headers=AUTH,
                    json={"openai_session": "v1/chatX", "target_session": "auto-x",
                          "target_org": "personal", "message": "the actual payload",
                          "intent": "coordinate the GIS pass"})
    body = r.json()
    assert body["status"] == "pending" and body["approval_id"]
    xrid = body["approval_id"]
    assert client.ar.rows[xrid]["request"]["message"] == "the actual payload"
    # non-popping status poll (what the relay uses while holding) -> still pending
    s = client.post("/api/mcp/crosstalk/status", headers=AUTH,
                    json={"openai_session": "v1/chatX", "target_session": "auto-x"})
    assert s.json()["status"] == "pending"
    # operator approves -> grant live -> status flips to approved
    client.ar.decide(xrid, {"approved": True})
    db.approve_crosstalk("v1/chatX", "auto-x", expires_at=None)
    s = client.post("/api/mcp/crosstalk/status", headers=AUTH,
                    json={"openai_session": "v1/chatX", "target_session": "auto-x"})
    assert s.json()["status"] == "approved"


# ── /api/mcp/crosstalk/relay — dashboard enforces AND delivers ─────────────

def _link(client, osession):
    """Link a chat to an org so it may message; returns its minted handle."""
    db.upsert_pending_session(osession)
    db.approve_session(osession, autonomy_org="autonomy", level="read", expires_at=None)
    return db.get_session(osession)["handle"]


def test_relay_rejects_an_unlinked_peer(client):
    r = client.post("/api/mcp/crosstalk/relay", headers=AUTH,
                    json={"openai_session": "v1/nope", "target_session": "auto-x",
                          "message": "hi", "intent": "coordinate"})
    assert r.json()["status"] == "peer_not_linked"  # link is a separate approval


def test_relay_holds_the_message_on_one_approval_when_ungranted(client):
    handle = _link(client, "v1/chatH")
    r = client.post("/api/mcp/crosstalk/relay", headers=AUTH,
                    json={"openai_session": "v1/chatH", "target_session": "auto-x",
                          "message": "the payload", "intent": "coordinate GIS"})
    body = r.json()
    assert body["status"] == "pending" and body["from"] == handle
    appr = client.ar.rows[body["approval_id"]]
    assert appr["session"] == handle                       # popup shows ChatGPT-<datetime>
    assert appr["request"]["message"] == "the payload"     # approval carries the message
    # a second relay of the same undecided send does NOT open a second popup
    client.post("/api/mcp/crosstalk/relay", headers=AUTH,
                json={"openai_session": "v1/chatH", "target_session": "auto-x",
                      "message": "the payload", "intent": "coordinate GIS"})
    assert len([a for a in client.ar.rows.values()
                if a["kind"] == "mcp_crosstalk"]) == 1


def test_relay_with_a_live_grant_delivers_stamped_from_the_handle(client):
    handle = _link(client, "v1/chatG")
    db.upsert_pending_crosstalk("v1/chatG", "auto-x")            # channel...
    db.approve_crosstalk("v1/chatG", "auto-x", expires_at=None)  # ...already open
    r = client.post("/api/mcp/crosstalk/relay", headers=AUTH,
                    json={"openai_session": "v1/chatG", "target_session": "auto-x",
                          "message": "flowing message", "intent": "coordinate"})
    body = r.json()
    assert body["status"] == "delivered" and body["from"] == handle
    # stored in the one message store, attributed to the handle (not any token)
    rows = auth_db.get_messages(session="auto-x")
    assert any(m["sender_session"] == handle and m["message"] == "flowing message"
               for m in rows)


def test_execute_crosstalk_delivers_the_held_message_from_the_handle(client):
    import asyncio
    handle = _link(client, "v1/chatE")
    db.upsert_pending_crosstalk("v1/chatE", "auto-x")
    row = {"request": {"openai_session": "v1/chatE", "target_session": "auto-x",
                       "message": "held then sent", "handle": handle}}
    out = asyncio.run(kinds.execute_crosstalk(row, {"ttl_seconds": 3600}))
    assert out["ok"] and out["from"] == handle
    rows = auth_db.get_messages(session="auto-x")
    assert any(m["sender_session"] == handle and m["message"] == "held then sent"
               for m in rows)


# ── /api/mcp/crosstalk/collect — a chat drains its own inbox ───────────────

def test_collect_returns_queued_replies_in_order_and_is_idempotent(client):
    handle = _link(client, "v1/chatC2")
    import time as _t
    auth_db.insert_message("auto-s", "planner", handle, None, None, "reply-1", _t.time(), 0)
    auth_db.insert_message("auto-s", "planner", handle, None, None, "reply-2", _t.time(), 0)
    r = client.post("/api/mcp/crosstalk/collect", headers=AUTH,
                    json={"openai_session": "v1/chatC2"})
    body = r.json()
    assert body["handle"] == handle
    assert [m["message"] for m in body["messages"]] == ["reply-1", "reply-2"]
    # collecting again returns nothing new (rows are now delivered)
    again = client.post("/api/mcp/crosstalk/collect", headers=AUTH,
                        json={"openai_session": "v1/chatC2"})
    assert again.json()["messages"] == []


def test_collect_unknown_session_is_empty_not_an_error(client):
    r = client.post("/api/mcp/crosstalk/collect", headers=AUTH,
                    json={"openai_session": "v1/never-helloed"})
    assert r.status_code == 200 and r.json()["messages"] == []
