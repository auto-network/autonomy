"""org-on-session-token (auto-zywlp): the token carries its owning org, minted
strictly, and an org-less token that belongs to a workspace is refused rather
than treated as a local caller.

The migration is deliberately un-backfilled: adding the column is product code,
populating it is not. A token minted before the column existed reads org=NULL and
— if its session has a workspace — is locked out until it re-mints on relaunch.
"""

from __future__ import annotations

import json
import sqlite3
import types

import pytest

from tools.dashboard.dao import auth_db


@pytest.fixture
def db(tmp_path):
    auth_db.init_db(tmp_path / "auth.db")
    yield tmp_path / "auth.db"
    if auth_db._conn is not None:
        auth_db._conn.close()
        auth_db._conn = None


def _hash(token: str) -> str:
    import hashlib
    return hashlib.sha256(token.encode()).hexdigest()


def test_insert_and_resolve_round_trips_org(db):
    auth_db.insert_token(_hash("container-tok"), "auto-1", "personal")
    assert auth_db.resolve_token(_hash("container-tok")) == ("auto-1", "personal")


def test_host_token_resolves_with_none_org(db):
    auth_db.insert_token(_hash("host-tok"), "host-1", None)
    assert auth_db.resolve_token(_hash("host-tok")) == ("host-1", None)


def test_unknown_and_revoked_tokens_resolve_to_none(db):
    assert auth_db.resolve_token(_hash("nope")) is None
    auth_db.insert_token(_hash("t"), "auto-2", "personal")
    auth_db.revoke_token("auto-2")
    assert auth_db.resolve_token(_hash("t")) is None


def test_legacy_db_without_org_column_is_altered_in_place(tmp_path):
    # A session_tokens table that predates the org column.
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE session_tokens (token_hash TEXT PRIMARY KEY, "
        "tmux_name TEXT NOT NULL, created_at REAL NOT NULL, revoked_at REAL)")
    conn.execute(
        "INSERT INTO session_tokens (token_hash, tmux_name, created_at) "
        "VALUES (?, ?, ?)", (_hash("old"), "auto-legacy", 1.0))
    conn.commit()
    conn.close()

    auth_db.init_db(path)  # must ALTER the column in, not raise
    try:
        cols = {r["name"] for r in auth_db._conn.execute(
            "PRAGMA table_info(session_tokens)").fetchall()}
        assert {"org", "kind", "expires_at", "service_scope"}.issubset(cols)
        # The pre-existing row is NOT backfilled: it reads org=None.
        assert auth_db.resolve_token(_hash("old")) == ("auto-legacy", None)
    finally:
        auth_db._conn.close()
        auth_db._conn = None


# ── authenticate_session_request: the reject-NULL guard ───────────────

def _req(auth_header: str | None):
    headers = {}
    if auth_header is not None:
        headers["authorization"] = auth_header
    return types.SimpleNamespace(headers=headers)


def _body(resp) -> dict:
    return json.loads(bytes(resp.body))


@pytest.fixture
def server_mod(monkeypatch):
    from tools.dashboard import server as mod
    return mod


def test_valid_container_token_returns_session_and_org(server_mod, monkeypatch):
    monkeypatch.setattr(server_mod.auth_db, "resolve_token",
                        lambda _h: ("auto-x", "personal"))
    identity, err = server_mod.authenticate_session_request(
        _req("Bearer whatever"))
    assert err is None
    assert identity == ("auto-x", "personal")


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER"])
def test_bearer_scheme_is_case_insensitive(server_mod, monkeypatch, scheme):
    monkeypatch.setattr(server_mod.auth_db, "resolve_token",
                        lambda _h: ("auto-x", "personal"))
    identity, err = server_mod.authenticate_session_request(
        _req(f"{scheme} whatever"))
    assert err is None
    assert identity == ("auto-x", "personal")


def test_missing_bearer_is_401(server_mod):
    for header in (None, "", "Bearer", "Bearer   ", "Basic abc"):
        identity, err = server_mod.authenticate_session_request(_req(header))
        assert identity is None
        assert err.status_code == 401


def test_unknown_token_is_401(server_mod, monkeypatch):
    monkeypatch.setattr(server_mod.auth_db, "resolve_token", lambda _h: None)
    identity, err = server_mod.authenticate_session_request(_req("Bearer x"))
    assert identity is None and err.status_code == 401


def test_orgless_agent_token_is_refused_not_local(server_mod, monkeypatch):
    # The reject-NULL guard: an org-less token that is not positively local is
    # refused, never treated as a local caller.
    monkeypatch.setattr(server_mod.auth_db, "resolve_token",
                        lambda _h: ("auto-legacy", None))
    monkeypatch.setattr(server_mod, "_is_local_caller", lambda _s: False)
    identity, err = server_mod.authenticate_session_request(_req("Bearer x"))
    assert identity is None
    assert err.status_code == 403
    assert "no organization" in _body(err)["error"]


def test_orgless_host_token_is_local(server_mod, monkeypatch):
    # A genuine host/local token (no org) resolves as local.
    monkeypatch.setattr(server_mod.auth_db, "resolve_token",
                        lambda _h: ("host-1", None))
    monkeypatch.setattr(server_mod, "_is_local_caller", lambda _s: True)
    identity, err = server_mod.authenticate_session_request(_req("Bearer x"))
    assert err is None
    assert identity == ("host-1", None)


def test_is_local_caller_requires_a_positive_host_assertion(server_mod, monkeypatch):
    from tools.dashboard.dao import dashboard_db
    rows = {
        "host-1": {"type": "host", "project": "-home-operator-workspace-checkout"},
        "auto-c": {"type": "container", "project": "autonomy-developer"},
        "auto-d": {"type": "dispatch", "project": "-workspace-repo"},
        "auto-l": {"type": "librarian", "project": "-workspace-repo"},
        "auto-a": {"type": "agentic", "project": "-workspace-repo"},
        "auto-t": {"type": "terminal", "project": "-workspace-repo"},
    }
    monkeypatch.setattr(dashboard_db, "get_session", lambda s: rows.get(s))
    # Only an existing type='host' row is local (even with a path-shaped project).
    assert server_mod._is_local_caller("host-1") is True
    for agent in ("auto-c", "auto-d", "auto-l", "auto-a", "auto-t"):
        assert server_mod._is_local_caller(agent) is False
    # Absence of evidence is NOT locality: no row / unknown / error all refuse.
    assert server_mod._is_local_caller("rowless-agent-token") is False  # the 1023 case
    monkeypatch.setattr(dashboard_db, "get_session",
                        lambda s: {"type": "weird-future-type"})
    assert server_mod._is_local_caller("x") is False
    monkeypatch.setattr(dashboard_db, "get_session",
                        lambda s: (_ for _ in ()).throw(RuntimeError("db down")))
    assert server_mod._is_local_caller("x") is False  # exception -> refuse, not allow
