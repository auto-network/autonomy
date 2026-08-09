"""Unit tests for the MCP-relay peer store DAO (temp DB, no dashboard needed)."""

import time

import pytest

from tools.dashboard.dao import mcp_relay_db as db


@pytest.fixture()
def dbp(tmp_path):
    return tmp_path / "mcp_relay.db"


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
    # pending until approved
    db.upsert_pending_crosstalk("v1/sessG", "auto-0809-130519", target_org="personal",
                                approval_id="x-1", db_path=dbp)
    assert db.crosstalk_allowed("v1/sessG", "auto-0809-130519", db_path=dbp) is False
    db.approve_crosstalk("v1/sessG", "auto-0809-130519", expires_at=time.time() + 3600,
                         db_path=dbp)
    assert db.crosstalk_allowed("v1/sessG", "auto-0809-130519", db_path=dbp) is True
    # a different target is a separate, ungranted pair
    assert db.crosstalk_allowed("v1/sessG", "auto-9999-000000", db_path=dbp) is False


def test_crosstalk_grant_expiry(dbp):
    db.upsert_pending_crosstalk("v1/sessH", "auto-x", db_path=dbp)
    db.approve_crosstalk("v1/sessH", "auto-x", expires_at=time.time() - 1, db_path=dbp)
    assert db.crosstalk_allowed("v1/sessH", "auto-x", db_path=dbp) is False
