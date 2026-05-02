"""Tests for sparse turn-correction persistence and accept/dismiss APIs.

Bead: auto-edec1.2. Covers DAO CRUD, REST endpoints (GET / accept / dismiss),
identity validation (session_uuid + target_message_id + original_sha256), and
the SessionMonitor hook used during live tail and warm-up replay to persist
``turn_correction`` parser events into ``dashboard.db``.

Parser-side upconversion of CLI output into ``turn_correction`` entries is
covered by ``tools/dashboard/tests/test_parser.py`` under bead auto-edec1.1.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from tools.dashboard.dao import dashboard_db


SESSION_UUID = "uuid-auto-test-designer"
TMUX_NAME = "auto-test-designer"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture
def client(test_app):
    with TestClient(test_app) as c:
        yield c


# ── DAO: create / pending ─────────────────────────────────────


def test_dao_upsert_creates_pending_row(test_app):
    sha = _sha("Jason encoded message")
    row = dashboard_db.upsert_turn_correction(
        SESSION_UUID, "msg-1",
        original_sha256=sha,
        corrected_text="JSON encoded message",
        mode="balanced",
        reason="dictation cleanup",
        confidence=0.9,
    )
    assert row["status"] == "pending"
    assert row["session_uuid"] == SESSION_UUID
    assert row["target_message_id"] == "msg-1"
    assert row["original_sha256"] == sha
    assert row["corrected_text"] == "JSON encoded message"
    assert row["mode"] == "balanced"
    assert row["reason"] == "dictation cleanup"
    assert row["confidence"] == pytest.approx(0.9)
    assert row["created_at"] > 0
    assert row["updated_at"] >= row["created_at"]


def test_dao_upsert_replaces_existing_pending(test_app):
    sha_v1 = _sha("Jason")
    dashboard_db.upsert_turn_correction(
        SESSION_UUID, "msg-1",
        original_sha256=sha_v1, corrected_text="JSON",
    )
    sha_v2 = _sha("Jay Son")
    row = dashboard_db.upsert_turn_correction(
        SESSION_UUID, "msg-1",
        original_sha256=sha_v2, corrected_text="JSON v2",
        mode="aggressive",
    )
    assert row["original_sha256"] == sha_v2
    assert row["corrected_text"] == "JSON v2"
    assert row["mode"] == "aggressive"
    # Still only one row for this key
    rows = dashboard_db.list_turn_corrections(SESSION_UUID)
    assert len([r for r in rows if r["target_message_id"] == "msg-1"]) == 1


def test_dao_upsert_does_not_overwrite_terminal(test_app):
    sha = _sha("text")
    dashboard_db.upsert_turn_correction(
        SESSION_UUID, "msg-1",
        original_sha256=sha, corrected_text="corrected",
    )
    dashboard_db.set_turn_correction_status(
        SESSION_UUID, "msg-1", "accepted", expected_sha256=sha,
    )
    new_sha = _sha("text v2")
    row = dashboard_db.upsert_turn_correction(
        SESSION_UUID, "msg-1",
        original_sha256=new_sha, corrected_text="corrected v2",
    )
    assert row["status"] == "accepted"
    assert row["original_sha256"] == sha
    assert row["corrected_text"] == "corrected"


def test_dao_get_returns_none_when_missing(test_app):
    assert dashboard_db.get_turn_correction(SESSION_UUID, "missing") is None


def test_dao_list_orders_by_created_at(test_app):
    dashboard_db.upsert_turn_correction(
        SESSION_UUID, "msg-a",
        original_sha256=_sha("a"), corrected_text="A",
    )
    dashboard_db.upsert_turn_correction(
        SESSION_UUID, "msg-b",
        original_sha256=_sha("b"), corrected_text="B",
    )
    rows = dashboard_db.list_turn_corrections(SESSION_UUID)
    assert [r["target_message_id"] for r in rows] == ["msg-a", "msg-b"]


def test_dao_validates_required_fields(test_app):
    with pytest.raises(ValueError):
        dashboard_db.upsert_turn_correction(
            "", "msg-1", original_sha256=_sha("x"), corrected_text="y",
        )
    with pytest.raises(ValueError):
        dashboard_db.upsert_turn_correction(
            SESSION_UUID, "", original_sha256=_sha("x"), corrected_text="y",
        )
    with pytest.raises(ValueError):
        dashboard_db.upsert_turn_correction(
            SESSION_UUID, "msg-1", original_sha256="", corrected_text="y",
        )


# ── DAO: accept / dismiss / identity validation ───────────────


def test_dao_accept_transitions_pending_to_accepted(test_app):
    sha = _sha("text")
    dashboard_db.upsert_turn_correction(
        SESSION_UUID, "msg-1",
        original_sha256=sha, corrected_text="corrected",
    )
    outcome, row = dashboard_db.set_turn_correction_status(
        SESSION_UUID, "msg-1", "accepted", expected_sha256=sha,
    )
    assert outcome == "ok"
    assert row["status"] == "accepted"


def test_dao_dismiss_transitions_pending_to_dismissed(test_app):
    sha = _sha("text")
    dashboard_db.upsert_turn_correction(
        SESSION_UUID, "msg-1",
        original_sha256=sha, corrected_text="corrected",
    )
    outcome, row = dashboard_db.set_turn_correction_status(
        SESSION_UUID, "msg-1", "dismissed", expected_sha256=sha,
    )
    assert outcome == "ok"
    assert row["status"] == "dismissed"


def test_dao_sha_mismatch_does_not_mutate_row(test_app):
    sha = _sha("text")
    dashboard_db.upsert_turn_correction(
        SESSION_UUID, "msg-1",
        original_sha256=sha, corrected_text="corrected",
    )
    outcome, row = dashboard_db.set_turn_correction_status(
        SESSION_UUID, "msg-1", "accepted",
        expected_sha256=_sha("different"),
    )
    assert outcome == "sha_mismatch"
    assert row is not None
    assert row["status"] == "pending"
    refreshed = dashboard_db.get_turn_correction(SESSION_UUID, "msg-1")
    assert refreshed["status"] == "pending"


def test_dao_already_terminal_returns_existing_status(test_app):
    sha = _sha("text")
    dashboard_db.upsert_turn_correction(
        SESSION_UUID, "msg-1",
        original_sha256=sha, corrected_text="corrected",
    )
    dashboard_db.set_turn_correction_status(
        SESSION_UUID, "msg-1", "accepted", expected_sha256=sha,
    )
    outcome, row = dashboard_db.set_turn_correction_status(
        SESSION_UUID, "msg-1", "dismissed", expected_sha256=sha,
    )
    assert outcome == "already_terminal"
    assert row["status"] == "accepted"


def test_dao_raced_terminal_update_returns_already_terminal(test_app, monkeypatch):
    """A losing concurrent transition must not report false success."""
    sha = _sha("text")
    dashboard_db.upsert_turn_correction(
        SESSION_UUID, "msg-1",
        original_sha256=sha, corrected_text="corrected",
    )
    base_conn = dashboard_db.get_conn()
    original_execute = base_conn.execute

    class RacingConn:
        def execute(self, sql, params=()):
            if sql.startswith("UPDATE turn_corrections SET status=?, updated_at=?"):
                # Simulate another caller accepting the row after our initial
                # SELECT but before this UPDATE executes.
                original_execute(
                    "UPDATE turn_corrections SET status='accepted' "
                    "WHERE session_uuid=? AND target_message_id=?",
                    (SESSION_UUID, "msg-1"),
                )
                base_conn.commit()
            return original_execute(sql, params)

        def commit(self):
            return base_conn.commit()

    monkeypatch.setattr(dashboard_db, "get_conn", lambda: RacingConn())
    outcome, row = dashboard_db.set_turn_correction_status(
        SESSION_UUID, "msg-1", "dismissed", expected_sha256=sha,
    )
    assert outcome == "already_terminal"
    assert row["status"] == "accepted"
    stored = dashboard_db.get_turn_correction(SESSION_UUID, "msg-1")
    assert stored["status"] == "accepted"


def test_dao_set_status_not_found(test_app):
    outcome, row = dashboard_db.set_turn_correction_status(
        SESSION_UUID, "no-such-msg", "accepted",
        expected_sha256=_sha("x"),
    )
    assert outcome == "not_found"
    assert row is None


def test_dao_set_status_rejects_non_terminal_target(test_app):
    sha = _sha("text")
    dashboard_db.upsert_turn_correction(
        SESSION_UUID, "msg-1",
        original_sha256=sha, corrected_text="corrected",
    )
    with pytest.raises(ValueError):
        dashboard_db.set_turn_correction_status(
            SESSION_UUID, "msg-1", "pending", expected_sha256=sha,
        )


def test_dao_keyed_per_session(test_app):
    """Same target_message_id under different sessions never collides."""
    sha = _sha("text")
    dashboard_db.upsert_turn_correction(
        SESSION_UUID, "msg-1",
        original_sha256=sha, corrected_text="A",
    )
    dashboard_db.upsert_turn_correction(
        "uuid-other-session", "msg-1",
        original_sha256=sha, corrected_text="B",
    )
    a = dashboard_db.get_turn_correction(SESSION_UUID, "msg-1")
    b = dashboard_db.get_turn_correction("uuid-other-session", "msg-1")
    assert a["corrected_text"] == "A"
    assert b["corrected_text"] == "B"


# ── API: GET ──────────────────────────────────────────────────


def test_api_list_empty_session(test_app, client):
    r = client.get(f"/api/session/{TMUX_NAME}/turn-corrections")
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == TMUX_NAME
    assert body["session_uuid"] == SESSION_UUID
    assert body["corrections"] == []


def test_api_list_returns_persisted_pending(test_app, client):
    sha = _sha("hi")
    dashboard_db.upsert_turn_correction(
        SESSION_UUID, "msg-1",
        original_sha256=sha, corrected_text="Hello",
        mode="conservative", reason="capitalize", confidence=0.85,
    )
    r = client.get(f"/api/session/{TMUX_NAME}/turn-corrections")
    body = r.json()
    assert len(body["corrections"]) == 1
    c = body["corrections"][0]
    assert c["status"] == "pending"
    assert c["target_message_id"] == "msg-1"
    assert c["original_sha256"] == sha
    assert c["corrected_text"] == "Hello"
    assert c["mode"] == "conservative"
    assert c["reason"] == "capitalize"
    assert c["confidence"] == pytest.approx(0.85)


def test_api_list_unknown_session_returns_404(test_app, client):
    r = client.get("/api/session/no-such-session/turn-corrections")
    assert r.status_code == 404


def test_api_list_accepts_session_uuid_in_path(test_app, client):
    sha = _sha("hi")
    dashboard_db.upsert_turn_correction(
        SESSION_UUID, "msg-1",
        original_sha256=sha, corrected_text="Hello",
    )
    r = client.get(f"/api/session/{SESSION_UUID}/turn-corrections")
    assert r.status_code == 200
    body = r.json()
    assert body["session_uuid"] == SESSION_UUID
    assert len(body["corrections"]) == 1


# ── API: accept ───────────────────────────────────────────────


def test_api_accept_pending_returns_accepted(test_app, client):
    sha = _sha("hi")
    dashboard_db.upsert_turn_correction(
        SESSION_UUID, "msg-1",
        original_sha256=sha, corrected_text="Hello",
    )
    r = client.post(
        f"/api/session/{TMUX_NAME}/turn-corrections/msg-1/accept",
        json={"original_sha256": sha},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["correction"]["status"] == "accepted"
    # Persisted as accepted
    stored = dashboard_db.get_turn_correction(SESSION_UUID, "msg-1")
    assert stored["status"] == "accepted"


def test_api_accept_stale_sha_returns_409(test_app, client):
    sha = _sha("hi")
    dashboard_db.upsert_turn_correction(
        SESSION_UUID, "msg-1",
        original_sha256=sha, corrected_text="Hello",
    )
    r = client.post(
        f"/api/session/{TMUX_NAME}/turn-corrections/msg-1/accept",
        json={"original_sha256": _sha("different")},
    )
    assert r.status_code == 409
    body = r.json()
    assert "stale" in body["error"]
    assert body["stored_sha256"] == sha
    # Row still pending
    stored = dashboard_db.get_turn_correction(SESSION_UUID, "msg-1")
    assert stored["status"] == "pending"


def test_api_accept_already_terminal_returns_409(test_app, client):
    sha = _sha("hi")
    dashboard_db.upsert_turn_correction(
        SESSION_UUID, "msg-1",
        original_sha256=sha, corrected_text="Hello",
    )
    dashboard_db.set_turn_correction_status(
        SESSION_UUID, "msg-1", "dismissed", expected_sha256=sha,
    )
    r = client.post(
        f"/api/session/{TMUX_NAME}/turn-corrections/msg-1/accept",
        json={"original_sha256": sha},
    )
    assert r.status_code == 409
    body = r.json()
    assert body["status"] == "dismissed"


def test_api_accept_missing_sha_returns_400(test_app, client):
    r = client.post(
        f"/api/session/{TMUX_NAME}/turn-corrections/msg-1/accept",
        json={},
    )
    assert r.status_code == 400


def test_api_accept_unknown_session_returns_404(test_app, client):
    r = client.post(
        "/api/session/no-such-session/turn-corrections/msg-1/accept",
        json={"original_sha256": _sha("x")},
    )
    assert r.status_code == 404


def test_api_accept_unknown_message_returns_404(test_app, client):
    r = client.post(
        f"/api/session/{TMUX_NAME}/turn-corrections/no-such-msg/accept",
        json={"original_sha256": _sha("x")},
    )
    assert r.status_code == 404


# ── API: dismiss ──────────────────────────────────────────────


def test_api_dismiss_pending_returns_dismissed(test_app, client):
    sha = _sha("hi")
    dashboard_db.upsert_turn_correction(
        SESSION_UUID, "msg-1",
        original_sha256=sha, corrected_text="Hello",
    )
    r = client.post(
        f"/api/session/{TMUX_NAME}/turn-corrections/msg-1/dismiss",
        json={"original_sha256": sha},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["correction"]["status"] == "dismissed"


def test_api_dismiss_stale_sha_returns_409(test_app, client):
    sha = _sha("hi")
    dashboard_db.upsert_turn_correction(
        SESSION_UUID, "msg-1",
        original_sha256=sha, corrected_text="Hello",
    )
    r = client.post(
        f"/api/session/{TMUX_NAME}/turn-corrections/msg-1/dismiss",
        json={"original_sha256": _sha("nope")},
    )
    assert r.status_code == 409


# ── Hydration / refresh ───────────────────────────────────────


def test_hydration_returns_mixed_status_rows(test_app, client):
    """A fresh GET after writes returns the persisted state — pending and terminal."""
    sha1 = _sha("hi")
    dashboard_db.upsert_turn_correction(
        SESSION_UUID, "msg-1",
        original_sha256=sha1, corrected_text="Hello",
    )
    dashboard_db.set_turn_correction_status(
        SESSION_UUID, "msg-1", "accepted", expected_sha256=sha1,
    )

    sha2 = _sha("bye")
    dashboard_db.upsert_turn_correction(
        SESSION_UUID, "msg-2",
        original_sha256=sha2, corrected_text="Goodbye",
    )

    sha3 = _sha("nope")
    dashboard_db.upsert_turn_correction(
        SESSION_UUID, "msg-3",
        original_sha256=sha3, corrected_text="Negative",
    )
    dashboard_db.set_turn_correction_status(
        SESSION_UUID, "msg-3", "dismissed", expected_sha256=sha3,
    )

    r = client.get(f"/api/session/{TMUX_NAME}/turn-corrections")
    assert r.status_code == 200
    body = r.json()
    assert len(body["corrections"]) == 3
    by_id = {c["target_message_id"]: c for c in body["corrections"]}
    assert by_id["msg-1"]["status"] == "accepted"
    assert by_id["msg-2"]["status"] == "pending"
    assert by_id["msg-3"]["status"] == "dismissed"
    # Sparse rows don't mutate transcript identity — original sha is preserved
    assert by_id["msg-1"]["original_sha256"] == sha1


def test_hydration_survives_fresh_client(test_app):
    """Simulate page reload by opening a new TestClient on the same app."""
    sha = _sha("hi")
    dashboard_db.upsert_turn_correction(
        SESSION_UUID, "msg-1",
        original_sha256=sha, corrected_text="Hello",
    )
    with TestClient(test_app) as c1:
        c1.post(
            f"/api/session/{TMUX_NAME}/turn-corrections/msg-1/accept",
            json={"original_sha256": sha},
        )

    with TestClient(test_app) as c2:
        r = c2.get(f"/api/session/{TMUX_NAME}/turn-corrections")
        assert r.status_code == 200
        body = r.json()
        assert len(body["corrections"]) == 1
        assert body["corrections"][0]["status"] == "accepted"


# ── SessionMonitor: replay/warm-up ────────────────────────────


def test_session_monitor_persists_turn_correction_entries(test_app):
    """The hook the live tail invokes upserts every well-formed event."""
    from tools.dashboard.session_monitor import SessionMonitor

    sha = _sha("Jason encoded")
    entries = [
        {"type": "user", "content": "Jason encoded", "message_id": "msg-1"},
        {
            "type": "turn_correction",
            "target_message_id": "msg-1",
            "original_sha256": sha,
            "corrected_text": "JSON encoded",
            "mode": "balanced",
            "confidence": 0.9,
            "reason": "dictation cleanup",
        },
    ]
    row = {"session_uuid": SESSION_UUID, "tmux_name": TMUX_NAME}
    SessionMonitor._persist_turn_corrections(row, entries)

    stored = dashboard_db.get_turn_correction(SESSION_UUID, "msg-1")
    assert stored is not None
    assert stored["status"] == "pending"
    assert stored["corrected_text"] == "JSON encoded"
    assert stored["mode"] == "balanced"
    assert stored["confidence"] == pytest.approx(0.9)
    assert stored["reason"] == "dictation cleanup"


def test_session_monitor_skips_non_correction_entries(test_app):
    from tools.dashboard.session_monitor import SessionMonitor

    entries = [
        {"type": "user", "content": "hi", "message_id": "msg-1"},
        {"type": "assistant", "content": "hello", "message_id": "msg-2"},
    ]
    row = {"session_uuid": SESSION_UUID}
    SessionMonitor._persist_turn_corrections(row, entries)
    assert dashboard_db.list_turn_corrections(SESSION_UUID) == []


def test_session_monitor_derives_uuid_from_jsonl_path(test_app):
    """Tail rows may arrive before row.session_uuid is populated."""
    from tools.dashboard.session_monitor import SessionMonitor

    sha = _sha("text")
    derived_uuid = "uuid-from-jsonl-path"
    entries = [{
        "type": "turn_correction",
        "target_message_id": "msg-1",
        "original_sha256": sha,
        "corrected_text": "corrected",
    }]
    row = {
        "session_uuid": None,
        "jsonl_path": str(Path("/tmp/sessions/autonomy") / f"{derived_uuid}.jsonl"),
    }
    SessionMonitor._persist_turn_corrections(row, entries)
    stored = dashboard_db.get_turn_correction(derived_uuid, "msg-1")
    assert stored is not None
    assert stored["status"] == "pending"
    assert stored["original_sha256"] == sha


def test_session_monitor_still_skips_when_no_stable_uuid_available(test_app):
    """If there is no explicit uuid, no JSONL path, and no tmux row, skip."""
    from tools.dashboard.session_monitor import SessionMonitor

    entries = [{
        "type": "turn_correction",
        "target_message_id": "msg-1",
        "original_sha256": _sha("text"),
        "corrected_text": "corrected",
    }]
    row = {"session_uuid": None}
    SessionMonitor._persist_turn_corrections(row, entries)
    assert dashboard_db.get_turn_correction("", "msg-1") is None


def test_session_monitor_skips_malformed_event(test_app):
    """Missing target_message_id / sha / corrected_text => silently dropped."""
    from tools.dashboard.session_monitor import SessionMonitor

    entries = [
        {  # missing target_message_id
            "type": "turn_correction",
            "original_sha256": _sha("x"), "corrected_text": "y",
        },
        {  # missing original_sha256
            "type": "turn_correction",
            "target_message_id": "msg-x", "corrected_text": "y",
        },
        {  # missing corrected_text
            "type": "turn_correction",
            "target_message_id": "msg-y", "original_sha256": _sha("x"),
        },
    ]
    row = {"session_uuid": SESSION_UUID}
    SessionMonitor._persist_turn_corrections(row, entries)
    assert dashboard_db.list_turn_corrections(SESSION_UUID) == []


def test_session_monitor_replay_does_not_mutate_terminal(test_app):
    """Replaying a history correction over an already-accepted row leaves it alone."""
    from tools.dashboard.session_monitor import SessionMonitor

    sha = _sha("text")
    dashboard_db.upsert_turn_correction(
        SESSION_UUID, "msg-1",
        original_sha256=sha, corrected_text="A",
    )
    dashboard_db.set_turn_correction_status(
        SESSION_UUID, "msg-1", "accepted", expected_sha256=sha,
    )

    # Re-emitting the same event during replay/warm-up
    row = {"session_uuid": SESSION_UUID, "tmux_name": TMUX_NAME}
    SessionMonitor._persist_turn_corrections(row, [{
        "type": "turn_correction",
        "target_message_id": "msg-1",
        "original_sha256": sha,
        "corrected_text": "A",
    }])
    stored = dashboard_db.get_turn_correction(SESSION_UUID, "msg-1")
    assert stored["status"] == "accepted"
