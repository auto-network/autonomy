"""The MCP relay mints a per-peer, org-scoped bearer on approval.

The chat's graph-CLI-backed tools (search/read/tail/sessions) hit the GENERAL
API, which the service token (scoped to /api/mcp/*) cannot reach. So approval
mints a real ORG_SESSION token stamped with the approved org, hands it back to
the relay via resolve/status, and revokes it the moment the binding is no longer
live-approved. Also pins host-0818's finding: an unapproved chat cannot drain
its outbox.
"""

from __future__ import annotations

import asyncio
import hashlib
import os

import pytest
from starlette.requests import Request

from tools.dashboard import mcp_peer_approvals
from tools.dashboard import mcp_relay_routes
from tools.dashboard.dao import auth_db
from tools.dashboard.dao import mcp_relay_db


@pytest.fixture
def dbs(tmp_path, monkeypatch):
    """Point both the relay DB and auth.db at temp files; restore auth's conn."""
    monkeypatch.setattr(mcp_relay_db, "DB_PATH", tmp_path / "mcp_relay.db")
    saved = auth_db._conn
    auth_db.init_db(tmp_path / "auth.db")
    try:
        yield
    finally:
        auth_db._conn = saved


def _pending(osession="chat-1"):
    mcp_relay_db.upsert_pending_session(osession, intent="work")
    return mcp_relay_db.ensure_handle(osession)


def test_relay_db_bearer_column_set_and_clear(dbs):
    _pending("chat-x")
    assert mcp_relay_db.set_session_bearer("chat-x", "raw-token") is True
    assert mcp_relay_db.get_session("chat-x")["peer_bearer"] == "raw-token"
    mcp_relay_db.set_session_bearer("chat-x", None)
    assert mcp_relay_db.get_session("chat-x")["peer_bearer"] is None


def test_approval_mints_an_org_scoped_bearer(dbs):
    handle = _pending("chat-1")
    row = {"request": {"openai_session": "chat-1"}}
    result = asyncio.run(mcp_peer_approvals.execute_link(
        row, {"autonomy_org": "autonomy", "level": "read"}))
    assert result["ok"] is True

    raw = mcp_relay_db.get_session("chat-1")["peer_bearer"]
    assert raw, "a bearer must be stored on approval"
    # It is a real, resolvable, ORG-SCOPED session token — identity = handle.
    resolved = auth_db.resolve_token(hashlib.sha256(raw.encode()).hexdigest())
    assert resolved == (handle, "autonomy")


def test_resolve_returns_the_bearer_only_while_approved(dbs):
    _pending("chat-1")
    asyncio.run(mcp_peer_approvals.execute_link(
        {"request": {"openai_session": "chat-1"}},
        {"autonomy_org": "autonomy", "level": "read"}))
    auth = mcp_relay_routes._authorization("chat-1")
    assert auth["status"] == mcp_relay_db.APPROVED
    assert auth["bearer"] == mcp_relay_db.get_session("chat-1")["peer_bearer"]


def test_reconcile_revokes_the_bearer_when_no_longer_approved(dbs):
    handle = _pending("chat-1")
    asyncio.run(mcp_peer_approvals.execute_link(
        {"request": {"openai_session": "chat-1"}},
        {"autonomy_org": "autonomy", "level": "read"}))
    raw = mcp_relay_db.get_session("chat-1")["peer_bearer"]
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    assert auth_db.resolve_token(token_hash) is not None

    # The grant is revoked; the next relay touch must kill the token.
    mcp_relay_db.set_session_status("chat-1", mcp_relay_db.REVOKED)
    mcp_relay_routes._reconcile("chat-1")

    assert mcp_relay_db.get_session("chat-1")["peer_bearer"] is None
    assert auth_db.resolve_token(token_hash) is None


def test_lazy_backfill_mints_a_bearer_for_a_preexisting_approval(dbs):
    # A session approved BEFORE mint-on-approval existed: approved, no bearer.
    handle = _pending("chat-old")
    mcp_relay_db.approve_session(
        "chat-old", autonomy_org="autonomy", level="readwrite", expires_at=None)
    assert mcp_relay_db.get_session("chat-old")["peer_bearer"] is None

    # The first approved poll self-heals: mints + returns + persists the bearer.
    auth = mcp_relay_routes._authorization("chat-old")
    raw = auth.get("bearer")
    assert raw, "resolve/status must lazily mint a bearer for a pre-existing approval"
    assert mcp_relay_db.get_session("chat-old")["peer_bearer"] == raw
    resolved = auth_db.resolve_token(hashlib.sha256(raw.encode()).hexdigest())
    assert resolved == (handle, "autonomy")


def _collect_request(osession, service_token):
    body = f'{{"openai_session": "{osession}"}}'.encode()
    return Request({
        "type": "http", "method": "POST", "path": "/api/mcp/crosstalk/collect",
        "query_string": b"",
        "headers": [
            (b"content-type", b"application/json"),
            (b"authorization", f"Bearer {service_token}".encode()),
        ],
        "state": {},
    }, receive=_body_receive(body))


def _body_receive(body: bytes):
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}
    return receive


def test_unapproved_chat_cannot_drain_its_outbox(dbs, monkeypatch):
    monkeypatch.setenv("MCP_RELAY_SERVICE_TOKEN", "svc-secret")
    _pending("chat-1")  # pending, never approved
    resp = asyncio.run(mcp_relay_routes.collect_crosstalk(
        _collect_request("chat-1", "svc-secret")))
    assert resp.status_code == 200
    import json
    assert json.loads(resp.body) == {"status": "peer_not_linked"}
