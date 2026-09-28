"""Unit tests for the MCP-relay peer store DAO (temp DB, no dashboard needed)."""

import sqlite3
import time

import pytest

from tools.dashboard.dao import mcp_relay_db as db


@pytest.fixture()
def dbp(tmp_path):
    return tmp_path / "mcp_relay.db"


# The pre-handle mcp_sessions schema — a real deployment created before the handle
# column existed. Every column CREATE_TABLES has today except `handle`.
_PRE_HANDLE_SCHEMA = """
CREATE TABLE mcp_sessions (
    openai_session   TEXT PRIMARY KEY,
    openai_subject   TEXT NOT NULL DEFAULT '',
    openai_org       TEXT NOT NULL DEFAULT '',
    intent           TEXT NOT NULL DEFAULT '',
    requested_org    TEXT NOT NULL DEFAULT '',
    autonomy_org     TEXT,
    level            TEXT,
    status           TEXT NOT NULL DEFAULT 'pending',
    approval_id      TEXT,
    expires_at       REAL,
    approved_by      TEXT,
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL
);
"""


def test_migration_adds_handle_to_a_preexisting_db(dbp):
    # A DB created before the handle column existed — the path a fresh-DB test
    # never exercises (the ordering bug that shipped: a handle index in the schema
    # script raised on this DB before the migration could add the column).
    conn = sqlite3.connect(str(dbp))
    conn.executescript(_PRE_HANDLE_SCHEMA)
    conn.execute("INSERT INTO mcp_sessions (openai_session, status, created_at,"
                 " updated_at) VALUES ('v1/old', 'approved', 0, 0)")
    conn.commit()
    conn.close()

    # Opening through the DAO must migrate (add handle + its index) without raising.
    fresh = db.upsert_pending_session("v1/new", db_path=dbp)
    assert fresh["handle"].startswith("ChatGPT-")           # new row gets a handle
    assert db.ensure_handle("v1/old", db_path=dbp).startswith("ChatGPT-")  # old row too
    # the index exists after migration
    conn = sqlite3.connect(str(dbp))
    names = {r[1] for r in conn.execute("PRAGMA index_list(mcp_sessions)")}
    conn.close()
    assert "idx_mcp_sessions_handle" in names


def test_hello_creates_pending_then_approve_binds(dbp):
    row = db.upsert_pending_session(
        "v1/sessA", openai_subject="v1/subj", openai_org="v1/oorg",
        intent="tunnel GIS help", requested_org="autonomy", approval_id="appr-1", db_path=dbp)
    assert row["status"] == db.PENDING
    assert row["intent"] == "tunnel GIS help"

    # unknown session resolves as unknown; this one is pending
    assert db.resolve_session("v1/never-seen", db_path=dbp)["status"] == "unknown"
    assert db.resolve_session("v1/sessA", db_path=dbp)["status"] == db.PENDING

    # approve binds to one org + level + TTL
    db.approve_session("v1/sessA", autonomy_org="autonomy", level="readwrite",
                       expires_at=time.time() + 3600, approved_by="operator", db_path=dbp)
    r = db.resolve_session("v1/sessA", db_path=dbp)
    assert r["status"] == db.APPROVED
    assert r["autonomy_org"] == "autonomy"
    assert r["level"] == "readwrite"


def test_expired_binding_reports_expired(dbp):
    db.upsert_pending_session("v1/sessB", db_path=dbp)
    db.approve_session("v1/sessB", autonomy_org="autonomy", level="read",
                       expires_at=time.time() - 1, db_path=dbp)  # already expired
    assert db.resolve_session("v1/sessB", db_path=dbp)["status"] == "expired"


def test_never_expires_when_ttl_none(dbp):
    db.upsert_pending_session("v1/sessC", db_path=dbp)
    db.approve_session("v1/sessC", autonomy_org="autonomy", level="read",
                       expires_at=None, db_path=dbp)
    assert db.resolve_session("v1/sessC", db_path=dbp)["status"] == db.APPROVED


def test_rehello_when_live_keeps_binding_when_not_live_resets(dbp):
    db.upsert_pending_session("v1/sessD", intent="first", db_path=dbp)
    db.approve_session("v1/sessD", autonomy_org="autonomy", level="read",
                       expires_at=time.time() + 3600, db_path=dbp)
    # re-hello while live -> binding preserved (caller decides whether to re-pop)
    row = db.upsert_pending_session("v1/sessD", intent="second", db_path=dbp)
    assert row["status"] == db.APPROVED
    assert row["intent"] == "first"  # unchanged while live

    # revoke -> now not live -> re-hello resets to pending with new intent
    db.set_session_status("v1/sessD", db.REVOKED, db_path=dbp)
    row = db.upsert_pending_session("v1/sessD", intent="third", requested_org="other", db_path=dbp)
    assert row["status"] == db.PENDING
    assert row["intent"] == "third"
    assert row["autonomy_org"] is None


def test_level_validation(dbp):
    db.upsert_pending_session("v1/sessE", db_path=dbp)
    with pytest.raises(ValueError):
        db.approve_session("v1/sessE", autonomy_org="autonomy", level="admin",
                           expires_at=None, db_path=dbp)


def test_revoke_kills_binding(dbp):
    db.upsert_pending_session("v1/sessF", db_path=dbp)
    db.approve_session("v1/sessF", autonomy_org="autonomy", level="readwrite",
                       expires_at=None, db_path=dbp)
    assert db.resolve_session("v1/sessF", db_path=dbp)["status"] == db.APPROVED
    db.set_session_status("v1/sessF", db.REVOKED, db_path=dbp)
    assert db.resolve_session("v1/sessF", db_path=dbp)["status"] == db.REVOKED


def test_crosstalk_grant_lifecycle(dbp):
    # pending until the decision waiting on it is settled
    db.upsert_pending_crosstalk("v1/sessG", "auto-0809-130519", target_org="personal",
                                approval_id="x-1", db_path=dbp)
    assert db.crosstalk_allowed("v1/sessG", "auto-0809-130519", db_path=dbp) is False
    db.settle_crosstalk("x-1", status=db.APPROVED, outcome="delivering",
                        expires_at=time.time() + 3600, db_path=dbp)
    assert db.crosstalk_allowed("v1/sessG", "auto-0809-130519", db_path=dbp) is True
    # a different target is a separate, ungranted pair
    assert db.crosstalk_allowed("v1/sessG", "auto-9999-000000", db_path=dbp) is False


def test_crosstalk_grant_expiry(dbp):
    db.upsert_pending_crosstalk("v1/sessH", "auto-x", approval_id="x-2", db_path=dbp)
    db.settle_crosstalk("x-2", status=db.APPROVED, outcome="delivering",
                        expires_at=time.time() - 1, db_path=dbp)
    assert db.crosstalk_allowed("v1/sessH", "auto-x", db_path=dbp) is False


def test_a_decision_settles_its_grant_once_with_its_outcome(dbp):
    db.upsert_pending_crosstalk("v1/sessI", "auto-x", approval_id="x-3", db_path=dbp)
    first = db.settle_crosstalk("x-3", status=db.APPROVED, outcome="delivering",
                                owner_pid=7, owner_start="s", db_path=dbp)
    assert first["openai_session"] == "v1/sessI" and first["approval_id"] == "x-3"
    assert db.settle_crosstalk("x-3", status=db.APPROVED, outcome="delivering",
                               db_path=dbp) is None
    assert db.get_crosstalk_grant("v1/sessI", "auto-x", db_path=dbp)["approval_id"] is None
    assert db.get_outcome("x-3", db_path=dbp)["state"] == "delivering"
    assert [r["approval_id"] for r in db.outcomes_in_state("delivering", db_path=dbp)] == ["x-3"]
    assert db.finish_outcome("x-3", "delivered", expect="delivering", db_path=dbp)
    assert not db.finish_outcome("x-3", "delivery_failed", expect="delivering", db_path=dbp)
    assert db.get_outcome("x-3", db_path=dbp)["state"] == "delivered"


def test_a_decision_for_a_replaced_approval_settles_nothing(dbp):
    db.upsert_pending_crosstalk("v1/sessJ", "auto-x", approval_id="old", db_path=dbp)
    db.upsert_pending_crosstalk("v1/sessJ", "auto-x", approval_id="new", db_path=dbp)
    assert db.settle_crosstalk("old", status=db.DENIED, outcome="declined", db_path=dbp) is None
    assert db.get_outcome("old", db_path=dbp) is None
    assert db.get_crosstalk_grant("v1/sessJ", "auto-x", db_path=dbp)["status"] == db.PENDING
