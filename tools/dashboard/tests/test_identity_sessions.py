"""Durable human-dashboard session store invariants."""

from __future__ import annotations

import pytest

from tools.dashboard.dao import identity_sessions


@pytest.fixture(autouse=True)
def session_db(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "DASHBOARD_IDENTITY_SESSION_DB", str(tmp_path / "identity-sessions.db")
    )
    identity_sessions.reset_for_tests()
    yield tmp_path / "identity-sessions.db"
    identity_sessions.reset_for_tests()


def _create(sid: str, *, method: str = "passkey",
            credential_id: str | None = "cred-1", created_at: int = 1000,
            expires_at: int = 2000, **extra):
    return identity_sessions.create_session(
        sid=sid,
        method=method,
        credential_id=credential_id,
        created_at=created_at,
        expires_at=expires_at,
        last_activity=created_at,
        user_agent=extra.pop("user_agent", "Browser/1"),
        source_ip=extra.pop("source_ip", "100.64.0.10"),
        **extra,
    )


def test_create_persists_security_and_approval_provenance(session_db):
    row = _create(
        "approval-1",
        method="approval",
        credential_id=None,
        grantee="host-0715-122549",
        scope=["dashboard:read", "dashboard:interact"],
    )
    assert row == {
        "sid": "approval-1",
        "method": "approval",
        "credential_id": None,
        "created_at": 1000,
        "last_activity": 1000.0,
        "user_agent": "Browser/1",
        "source_ip": "100.64.0.10",
        "status": "active",
        "end_reason": None,
        "ended_at": None,
        "expires_at": 2000,
        "grantee": "host-0715-122549",
        "scope": ["dashboard:read", "dashboard:interact"],
    }
    identity_sessions.reset_for_tests()
    assert identity_sessions.get_session("approval-1", now=1001) == row


def test_active_check_matches_signed_provenance_and_throttles_touch():
    _create("sid-1")
    assert identity_sessions.check_active(
        sid="sid-1", method="passkey", created_at=1000,
        expires_at=2000, now=1059,
    ) is True
    assert identity_sessions.get_session("sid-1", now=1059)["last_activity"] == 1000
    assert identity_sessions.check_active(
        sid="sid-1", method="passkey", created_at=1000,
        expires_at=2000, now=1060,
    ) is True
    assert identity_sessions.get_session("sid-1", now=1060)["last_activity"] == 1060
    for mismatch in (
        {"method": "password", "created_at": 1000, "expires_at": 2000},
        {"method": "passkey", "created_at": 999, "expires_at": 2000},
        {"method": "passkey", "created_at": 1000, "expires_at": 2001},
    ):
        assert identity_sessions.check_active(
            sid="sid-1", now=1061, **mismatch,
        ) is False
    assert identity_sessions.check_active(
        sid="missing", method="passkey", created_at=1000,
        expires_at=2000, now=1061,
    ) is False


def test_hot_verification_reuses_connection_and_runs_one_indexed_lookup():
    _create("hot-path")
    conn, _lock = identity_sessions._open_pooled(identity_sessions.db_path())
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        assert identity_sessions.check_active(
            sid="hot-path", method="passkey", created_at=1000,
            expires_at=2000, now=1001,
        ) is True
    finally:
        conn.set_trace_callback(None)
    sql = [statement for statement in statements if statement.strip()]
    assert len([statement for statement in sql
                if statement.lstrip().upper().startswith("SELECT")]) == 1
    assert not any("CREATE TABLE" in statement.upper() for statement in sql)
    assert identity_sessions._open_pooled(identity_sessions.db_path())[0] is conn


def test_lock_ends_only_one_session_and_history_is_retained():
    _create("current")
    _create("other")
    assert identity_sessions.end_session("current", now=1100) is True
    assert identity_sessions.end_session("current", now=1101) is False
    current = identity_sessions.get_session("current", now=1101)
    other = identity_sessions.get_session("other", now=1101)
    assert (current["status"], current["end_reason"], current["ended_at"]) == (
        "ended", "locked", 1100,
    )
    assert other["status"] == "active"


def test_passkey_revoke_cascades_but_password_session_survives():
    _create("passkey-a", credential_id="shared-passkey", created_at=1000)
    _create("passkey-b", credential_id="shared-passkey", created_at=1001)
    _create("other-passkey", credential_id="other", created_at=1002)
    _create("password", method="password", credential_id=None, created_at=1003)

    assert identity_sessions.revoke_credential_sessions(
        "shared-passkey", now=1200
    ) == 2
    for sid in ("passkey-a", "passkey-b"):
        row = identity_sessions.get_session(sid, now=1200)
        assert row["status"] == "revoked"
        assert row["end_reason"] == "passkey_revoked"
        assert identity_sessions.check_active(
            sid=sid, method="passkey", created_at=row["created_at"],
            expires_at=row["expires_at"], now=1200,
        ) is False
    assert identity_sessions.get_session("other-passkey", now=1200)["status"] == "active"
    assert identity_sessions.get_session("password", now=1200)["status"] == "active"


def test_history_returns_all_active_and_only_ten_latest_ended():
    _create("active", credential_id="cred-history", created_at=1000,
            expires_at=5000)
    for index in range(12):
        sid = f"ended-{index:02d}"
        _create(sid, credential_id="cred-history", created_at=1001 + index,
                expires_at=5000)
        identity_sessions.revoke_session(sid, now=2000 + index)
    rows = identity_sessions.sessions_for_credential(
        "cred-history", now=3000,
    )
    assert [row["sid"] for row in rows["active"]] == ["active"]
    assert [row["sid"] for row in rows["recent_ended"]] == [
        f"ended-{index:02d}" for index in range(11, 1, -1)
    ]


def test_expiry_is_recorded_and_refused():
    _create("expired")
    assert identity_sessions.check_active(
        sid="expired", method="passkey", created_at=1000,
        expires_at=2000, now=2000,
    ) is False
    row = identity_sessions.get_session("expired", now=2001)
    assert row["status"] == "expired"
    assert row["end_reason"] == "expired"
    assert row["ended_at"] == 2000


def test_store_errors_are_typed_failures(tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("occupied")
    monkeypatch.setenv(
        "DASHBOARD_IDENTITY_SESSION_DB", str(blocker / "sessions.db")
    )
    identity_sessions.reset_for_tests()
    with pytest.raises(identity_sessions.SessionStoreError,
                       match="initialize session store"):
        _create("sid-1")


@pytest.mark.parametrize("bad_scope", [{1, 2}, object()])
def test_scope_must_be_json_serializable(bad_scope):
    with pytest.raises(ValueError, match="JSON-serializable"):
        _create("bad-scope", scope=bad_scope)
