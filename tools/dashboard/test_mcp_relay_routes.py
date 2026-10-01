"""Integration tests for the relay-facing /api/mcp/* routes.

Exercises the route logic (service-token auth, pending link-approval creation,
approve/decline reconciliation, crosstalk collection) via Starlette's TestClient
against a temp DB and a stubbed approval store — no full dashboard needed. The
Central mcp_crosstalk flow is in tests/test_mcp_crosstalk_central.py.
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
    monkeypatch.setattr(routes, "_expected_service_token", lambda: TOKEN)
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


def test_an_unreleased_token_is_fail_closed(client, monkeypatch, tmp_path):
    """No released token (the vault is cold, or never sealed): 503."""
    monkeypatch.undo()
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "mcp_relay.db")
    monkeypatch.setenv("AUTONOMY_KEYCACHE_MOUNT", str(tmp_path / "keycache"))
    monkeypatch.setenv("MCP_RELAY_SERVICE_TOKEN", TOKEN)     # no longer a source
    r = client.post("/api/mcp/session/resolve", headers=AUTH,
                    json={"openai_session": "v1/s"})
    assert r.status_code == 503
    assert "mcp-relay.service-token" in r.json()["error"]


def test_the_token_is_the_released_copy(client, monkeypatch, tmp_path):
    monkeypatch.undo()
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "mcp_relay.db")
    released = tmp_path / "keycache" / routes.RELAY_RELEASE_SUBDIR
    released.mkdir(parents=True)
    (released / routes.SERVICE_TOKEN_FILE).write_text(TOKEN + "\n")
    monkeypatch.setenv("AUTONOMY_KEYCACHE_MOUNT", str(tmp_path / "keycache"))
    assert routes._expected_service_token() == TOKEN


def test_the_relay_env_file_is_never_read():
    import inspect

    source = inspect.getsource(routes)
    assert "relay.env" not in source.replace("plaintext data/services/mcp-relay/relay.env", "")
    assert "SERVICE_TOKEN_ENV" not in source


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


# mcp_crosstalk approvals, grants and delivery: tests/test_mcp_crosstalk_central.py


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
    # Not an error, and nothing drains: a chat never linked (not even helloed)
    # reads as not linked, like the sibling resolve/relay routes.
    assert r.status_code == 200
    assert r.json() == {"status": "peer_not_linked"}


# ── releasing the relay's credentials from the vault (auto-5gdao) ──────────

def _vault(monkeypatch, *, warm=True, rows=None):
    from tools.graph import settings_ops

    rows = {"mcp-relay.service-token": {"payload": {"value": "svc\n"}},
            "mcp-relay.control-plane-api-key": {"payload": {"value": "sk-x"}}} \
        if rows is None else rows
    monkeypatch.setattr(settings_ops, "personal_delegate_audited_is_warm", lambda: warm)
    monkeypatch.setattr(settings_ops, "read_set_key",
                        lambda set_id, key, *, org, peers=None: rows.get(key))


def test_release_writes_both_values_for_the_relay(monkeypatch, tmp_path):
    import stat

    _vault(monkeypatch)
    d = tmp_path / "mcp-relay"
    assert routes.release_relay_credentials(directory=d, memory_check=lambda p: None) == "ok"
    assert (d / "service-token").read_text() == "svc"
    assert (d / "control-plane-api-key").read_text() == "sk-x"
    assert all(stat.S_IMODE(f.stat().st_mode) == 0o600 for f in d.iterdir())


def test_a_cold_vault_keeps_the_released_values(monkeypatch, tmp_path):
    _vault(monkeypatch)
    d = tmp_path / "mcp-relay"
    routes.release_relay_credentials(directory=d, memory_check=lambda p: None)
    _vault(monkeypatch, warm=False)
    assert routes.release_relay_credentials(directory=d, memory_check=lambda p: None) == "vault-cold"
    assert (d / "service-token").exists()


def test_an_unsealed_value_clears_the_release(monkeypatch, tmp_path):
    _vault(monkeypatch)
    d = tmp_path / "mcp-relay"
    routes.release_relay_credentials(directory=d, memory_check=lambda p: None)
    _vault(monkeypatch, rows={"mcp-relay.service-token": {"payload": {"value": "svc"}}})
    assert routes.release_relay_credentials(directory=d, memory_check=lambda p: None) == "unsealed"
    assert list(d.iterdir()) == []
