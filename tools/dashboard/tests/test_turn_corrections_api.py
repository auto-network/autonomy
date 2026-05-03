"""Tests for sparse turn-correction persistence and accept/dismiss APIs.

Bead: auto-edec1.2. Covers DAO CRUD, REST endpoints (GET / accept / dismiss),
identity validation (session_uuid + target_message_id + original_sha256), and
the SessionMonitor hook used during live tail and warm-up replay to persist
``turn_correction`` parser events into ``dashboard.db``.

Parser-side upconversion of CLI output into unresolved ``turn_correction``
entries is covered by ``tools/dashboard/tests/test_parser.py`` under bead
auto-edec1.1. SessionMonitor resolves those entries onto the most likely user
turn before persisting them here.
"""

from __future__ import annotations

import hashlib
import json
import pytest
from starlette.testclient import TestClient

from tools.dashboard.dao import dashboard_db
from tools.dashboard.session_harness import parse_codex_log_line


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
    assert r.headers["cache-control"] == "no-store"
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
    """Unresolved turn-correction entries bind to the prior user turn."""
    from tools.dashboard.session_monitor import SessionMonitor, _TailState

    entries = [
        {"type": "user", "content": "Jason encoded", "message_id": "msg-1"},
        {
            "type": "turn_correction",
            "corrected_text": "JSON encoded",
            "mode": "balanced",
            "confidence": 0.9,
            "reason": "dictation cleanup",
        },
    ]
    row = {"session_uuid": SESSION_UUID, "tmux_name": TMUX_NAME}
    ts = _TailState()
    SessionMonitor._persist_turn_corrections(row, ts, entries)

    stored = dashboard_db.get_turn_correction(SESSION_UUID, "msg-1")
    assert stored is not None
    assert stored["status"] == "pending"
    assert stored["target_message_id"] == "msg-1"
    assert stored["original_sha256"] == _sha("Jason encoded")
    assert stored["corrected_text"] == "JSON encoded"
    assert stored["mode"] == "balanced"
    assert stored["confidence"] == pytest.approx(0.9)
    assert stored["reason"] == "dictation cleanup"
    assert entries[1]["target_message_id"] == "msg-1"
    assert entries[1]["original_sha256"] == _sha("Jason encoded")


def test_session_monitor_skips_non_correction_entries(test_app):
    from tools.dashboard.session_monitor import SessionMonitor, _TailState

    entries = [
        {"type": "user", "content": "hi", "message_id": "msg-1"},
        {"type": "assistant", "content": "hello", "message_id": "msg-2"},
    ]
    row = {"session_uuid": SESSION_UUID}
    SessionMonitor._persist_turn_corrections(row, _TailState(), entries)
    assert dashboard_db.list_turn_corrections(SESSION_UUID) == []


def test_session_monitor_skips_session_without_uuid(test_app):
    """Session still resolving — the helper defends by skipping persistence."""
    from tools.dashboard.session_monitor import SessionMonitor, _TailState

    entries = [{
        "type": "user",
        "content": "text",
        "message_id": "msg-1",
    }, {
        "type": "turn_correction",
        "corrected_text": "corrected",
    }]
    row = {"session_uuid": None}
    SessionMonitor._persist_turn_corrections(row, _TailState(), entries)
    assert dashboard_db.get_turn_correction("", "msg-1") is None


def test_session_monitor_skips_malformed_event(test_app):
    """Missing corrected_text or no resolvable user target => silently dropped."""
    from tools.dashboard.session_monitor import SessionMonitor, _TailState

    entries = [
        {  # no preceding/following user turn to resolve against
            "type": "turn_correction",
            "corrected_text": "y",
        },
        {  # missing corrected_text
            "type": "turn_correction",
            "mode": "balanced",
        },
    ]
    row = {"session_uuid": SESSION_UUID}
    SessionMonitor._persist_turn_corrections(row, _TailState(), entries)
    assert dashboard_db.list_turn_corrections(SESSION_UUID) == []


def test_session_monitor_uses_recent_history_when_correction_arrives_next_tail_pass(test_app):
    from tools.dashboard.session_monitor import SessionMonitor, _TailState

    row = {"session_uuid": SESSION_UUID, "tmux_name": TMUX_NAME}
    ts = _TailState()

    SessionMonitor._persist_turn_corrections(
        row,
        ts,
        [{
            "type": "user",
            "content": "Jason encoded",
            "message_id": "msg-1",
            "timestamp": "2026-05-03T08:57:01.000Z",
        }],
    )
    SessionMonitor._persist_turn_corrections(
        row,
        ts,
        [{
            "type": "turn_correction",
            "corrected_text": "JSON encoded",
            "timestamp": "2026-05-03T08:57:02.000Z",
        }],
    )

    stored = dashboard_db.get_turn_correction(SESSION_UUID, "msg-1")
    assert stored is not None
    assert stored["original_sha256"] == _sha("Jason encoded")
    assert stored["corrected_text"] == "JSON encoded"


def test_session_monitor_prefers_cleanest_nearby_candidate_over_nearest_prior(test_app):
    from tools.dashboard.session_monitor import SessionMonitor, _TailState

    row = {"session_uuid": SESSION_UUID, "tmux_name": TMUX_NAME}
    ts = _TailState()
    SessionMonitor._persist_turn_corrections(row, ts, [
        {
            "type": "user",
            "content": "I'm gonna write a message which you can away a correction to.",
            "message_id": "msg-typo",
            "timestamp": "2026-05-03T08:57:01.000Z",
        },
        {
            "type": "user",
            "content": "Time I'm gonna put it two messages back",
            "message_id": "msg-middle",
            "timestamp": "2026-05-03T08:57:02.000Z",
        },
        {
            "type": "user",
            "content": "So now try to apply the correction and we'll see if it can match it",
            "message_id": "msg-nearest",
            "timestamp": "2026-05-03T08:57:03.000Z",
        },
    ])
    SessionMonitor._persist_turn_corrections(row, ts, [{
        "type": "turn_correction",
        "corrected_text": "I'm gonna write a message which you can apply a correction to.",
        "timestamp": "2026-05-03T08:57:04.000Z",
    }])

    stored = dashboard_db.get_turn_correction(SESSION_UUID, "msg-typo")
    assert stored is not None
    assert stored["original_sha256"] == _sha("I'm gonna write a message which you can away a correction to.")
    assert stored["corrected_text"] == "I'm gonna write a message which you can apply a correction to."
    assert dashboard_db.get_turn_correction(SESSION_UUID, "msg-nearest") is None


def test_turn_correction_metrics_accepts_long_dictation_cleanup():
    from tools.dashboard.session_monitor import _turn_correction_metrics

    raw = (
        "It worked and updated live. Congratulations I think the future is finally almost landed.\n\n"
        "Now the next thing to do is product conversation, which means primers which explain the command "
        "and explain when it should be used, which we want to biased towards using it aggressively I would "
        "not be angry if almost every single one of my messages got a correction if it meant cleaning up the log.\n\n"
        "Next effort products I want to go back and look at the source viewer so I want to see if corrections "
        "actually show up when we viewed them in the source viewer and I know that they won’t and the source "
        "view needs a lot of work because most of the time it doesn’t even know the difference between a "
        "assistant turn in a user turn even that doesn’t render properly so that’ll be the next thing we work "
        "on after this is productized"
    )
    corrected = (
        "It worked and updated live. Congratulations. I think the future is finally almost landed.\n\n"
        "Now the next thing to do is product conversation, which means primers that explain the command and "
        "explain when it should be used. We want to be biased toward using it aggressively. I would not be "
        "angry if almost every single one of my messages got a correction if it meant cleaning up the log.\n\n"
        "Next effort: productize. I want to go back and look at the source viewer. I want to see whether "
        "corrections actually show up when we view them in the source viewer, and I know they won’t. The "
        "source viewer needs a lot of work because most of the time it doesn’t even know the difference "
        "between an assistant turn and a user turn. Even that doesn’t render properly. So that’ll be the "
        "next thing we work on after this is productized."
    )

    metrics = _turn_correction_metrics(raw, corrected)
    assert metrics["acceptable"] is True
    assert metrics["char_similarity"] >= 0.80


def test_turn_correction_metrics_rejects_unrelated_message():
    from tools.dashboard.session_monitor import _turn_correction_metrics

    raw = "Proceed"
    corrected = (
        "It worked and updated live. Congratulations. I think the future is finally almost landed.\n\n"
        "Now the next thing to do is product conversation, which means primers that explain the command."
    )

    metrics = _turn_correction_metrics(raw, corrected)
    assert metrics["acceptable"] is False
    assert metrics["char_similarity"] < 0.25


def test_turn_correction_metrics_prefers_related_long_message_over_unrelated_short_one():
    from tools.dashboard.session_monitor import _turn_correction_metrics

    good_raw = "I’m gonna write a message which you can away a correction to."
    bad_raw = "So now try to apply the correction and we’ll see if it can match it"
    corrected = "I’m gonna write a message which you can apply a correction to."

    good = _turn_correction_metrics(good_raw, corrected)
    bad = _turn_correction_metrics(bad_raw, corrected)
    assert good["score_key"] < bad["score_key"]


def test_session_monitor_skips_stale_recent_history_candidates(test_app):
    from tools.dashboard.session_monitor import SessionMonitor, _TailState

    row = {"session_uuid": SESSION_UUID, "tmux_name": TMUX_NAME}
    ts = _TailState()

    SessionMonitor._persist_turn_corrections(
        row,
        ts,
        [{
            "type": "user",
            "content": "Jason encoded",
            "message_id": "msg-1",
            "timestamp": "2026-05-03T08:00:00.000Z",
        }],
    )
    SessionMonitor._persist_turn_corrections(
        row,
        ts,
        [{
            "type": "turn_correction",
            "corrected_text": "JSON encoded",
            "timestamp": "2026-05-03T08:06:01.000Z",
        }],
    )

    assert dashboard_db.get_turn_correction(SESSION_UUID, "msg-1") is None


def test_session_monitor_matches_long_productization_cleanup(test_app):
    from tools.dashboard.session_monitor import SessionMonitor, _TailState

    row = {"session_uuid": SESSION_UUID, "tmux_name": TMUX_NAME}
    ts = _TailState()

    raw = (
        "It worked and updated live. Congratulations I think the future is finally almost landed.\n\n"
        "Now the next thing to do is product conversation, which means primers which explain the command "
        "and explain when it should be used, which we want to biased towards using it aggressively I would "
        "not be angry if almost every single one of my messages got a correction if it meant cleaning up the log.\n\n"
        "Next effort products I want to go back and look at the source viewer so I want to see if corrections "
        "actually show up when we viewed them in the source viewer and I know that they won’t and the source "
        "view needs a lot of work because most of the time it doesn’t even know the difference between a "
        "assistant turn in a user turn even that doesn’t render properly so that’ll be the next thing we work "
        "on after this is productized"
    )
    corrected = (
        "It worked and updated live. Congratulations. I think the future is finally almost landed.\n\n"
        "Now the next thing to do is product conversation, which means primers that explain the command and "
        "explain when it should be used. We want to be biased toward using it aggressively. I would not be "
        "angry if almost every single one of my messages got a correction if it meant cleaning up the log.\n\n"
        "Next effort: productize. I want to go back and look at the source viewer. I want to see whether "
        "corrections actually show up when we view them in the source viewer, and I know they won’t. The "
        "source viewer needs a lot of work because most of the time it doesn’t even know the difference "
        "between an assistant turn and a user turn. Even that doesn’t render properly. So that’ll be the "
        "next thing we work on after this is productized."
    )

    SessionMonitor._persist_turn_corrections(row, ts, [
        {
            "type": "user",
            "content": "For the rest of this session, please be aggressive about issuing corrections.",
            "message_id": "msg-other-1",
            "timestamp": "2026-05-03T22:09:45.064Z",
        },
        {
            "type": "user",
            "content": "Proceed",
            "message_id": "msg-other-2",
            "timestamp": "2026-05-03T22:10:59.637Z",
        },
        {
            "type": "user",
            "content": "All right, here’s one short typo message for you to corruct",
            "message_id": "msg-other-3",
            "timestamp": "2026-05-03T22:12:00.173Z",
        },
        {
            "type": "user",
            "content": raw,
            "message_id": "msg-target",
            "timestamp": "2026-05-03T22:13:26.551Z",
        },
    ])
    SessionMonitor._persist_turn_corrections(row, ts, [{
        "type": "turn_correction",
        "corrected_text": corrected,
        "timestamp": "2026-05-03T22:13:45.008Z",
    }])

    stored = dashboard_db.get_turn_correction(SESSION_UUID, "msg-target")
    assert stored is not None
    assert stored["corrected_text"] == corrected


def test_session_monitor_matches_long_stop_message_cleanup(test_app):
    from tools.dashboard.session_monitor import SessionMonitor, _TailState

    row = {"session_uuid": SESSION_UUID, "tmux_name": TMUX_NAME}
    ts = _TailState()

    raw = (
        "Wait, stop what you’re saying makes no sense. You’re going at it again motherfucker\n\n"
        "I think you’re confused by the fact that you issued a correction after the one that fail failed "
        "which I accepted and it worked fine. We’re talking about the really long correction to the longer "
        "message that didn’t match and I didn’t accept it. I never even saw it displayed please try to keep"
    )
    corrected = (
        "Wait, stop. What you’re saying makes no sense. You’re doing it again, motherfucker.\n\n"
        "I think you’re confused by the fact that you issued a correction after the one that failed, which "
        "I accepted, and it worked fine. We’re talking about the really long correction to the longer "
        "message that didn’t match, and I didn’t accept it. I never even saw it displayed. Please try to "
        "keep that straight."
    )

    SessionMonitor._persist_turn_corrections(row, ts, [
        {
            "type": "user",
            "content": "Proceed",
            "message_id": "msg-other-1",
            "timestamp": "2026-05-03T22:10:59.637Z",
        },
        {
            "type": "user",
            "content": "That one failed to match sad face",
            "message_id": "msg-other-2",
            "timestamp": "2026-05-03T22:14:02.991Z",
        },
        {
            "type": "user",
            "content": raw,
            "message_id": "msg-target",
            "timestamp": "2026-05-03T22:16:10.709Z",
        },
    ])
    SessionMonitor._persist_turn_corrections(row, ts, [{
        "type": "turn_correction",
        "corrected_text": corrected,
        "timestamp": "2026-05-03T22:16:30.599Z",
    }])

    stored = dashboard_db.get_turn_correction(SESSION_UUID, "msg-target")
    assert stored is not None
    assert stored["corrected_text"] == corrected


def test_session_monitor_limits_matching_to_last_five_live_user_messages(test_app):
    from tools.dashboard.session_monitor import SessionMonitor, _TailState

    row = {"session_uuid": SESSION_UUID, "tmux_name": TMUX_NAME}
    ts = _TailState()

    SessionMonitor._persist_turn_corrections(row, ts, [
        {
            "type": "user",
            "content": "message zero with the original typo",
            "message_id": "msg-0",
            "timestamp": "2026-05-03T08:57:00.000Z",
        },
        {
            "type": "user",
            "content": "alpha filler about deployment logs",
            "message_id": "msg-1",
            "timestamp": "2026-05-03T08:57:01.000Z",
        },
        {
            "type": "user",
            "content": "beta filler about session cards",
            "message_id": "msg-2",
            "timestamp": "2026-05-03T08:57:02.000Z",
        },
        {
            "type": "user",
            "content": "gamma filler about dashboard css",
            "message_id": "msg-3",
            "timestamp": "2026-05-03T08:57:03.000Z",
        },
        {
            "type": "user",
            "content": "delta filler about graph search",
            "message_id": "msg-4",
            "timestamp": "2026-05-03T08:57:04.000Z",
        },
        {
            "type": "user",
            "content": "epsilon filler about source viewer",
            "message_id": "msg-5",
            "timestamp": "2026-05-03T08:57:05.000Z",
        },
    ])
    SessionMonitor._persist_turn_corrections(
        row,
        ts,
        [{
            "type": "turn_correction",
            "corrected_text": "message zero with the original fix",
            "timestamp": "2026-05-03T08:57:06.000Z",
        }],
    )

    assert dashboard_db.list_turn_corrections(SESSION_UUID) == []


def test_session_monitor_history_replay_does_not_warm_live_user_deque(test_app):
    from tools.dashboard.session_monitor import SessionMonitor, _TailState

    row = {"session_uuid": SESSION_UUID, "tmux_name": TMUX_NAME}
    ts = _TailState()

    SessionMonitor._persist_turn_corrections(
        row,
        ts,
        [{
            "type": "user",
            "content": "Jason encoded",
            "message_id": "msg-1",
            "timestamp": "2026-05-03T08:57:01.000Z",
        }],
        remember_users=False,
    )

    assert list(ts.recent_user_turns) == []


def test_session_monitor_persists_codex_event_message_correction_without_raw_uuid(test_app):
    """Codex event_msg user turns without raw UUID still get a stable target id."""
    from tools.dashboard.session_monitor import SessionMonitor, _TailState

    raw_entries = [
        {
            "timestamp": "2026-05-03T08:57:01.000Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "Jason encoded"},
        },
        {
            "timestamp": "2026-05-03T08:57:02.000Z",
            "type": "event_msg",
            "payload": {
                "type": "exec_command_end",
                "call_id": "call_tc_blank",
                "aggregated_output": "",
                "stdout": "",
                "stderr": "",
                "exit_code": 0,
                "status": "completed",
                "cwd": "/workspace/repo",
                "parsed_cmd": [{"type": "unknown", "cmd": (
                    'graph turn-correction suggest "JSON encoded" '
                    '--mode balanced --reason "dictation cleanup" --json'
                )}],
                "command": [
                    "bash",
                    "-lc",
                    'graph turn-correction suggest "JSON encoded" '
                    '--mode balanced --reason "dictation cleanup" --json',
                ],
                "duration": {"secs": 0, "nanos": 125_000_000},
                "process_id": 4243,
            },
        },
    ]
    entries = []
    for raw in raw_entries:
        parsed = parse_codex_log_line(json.dumps(raw))
        if isinstance(parsed, list):
            entries.extend(parsed)
        elif parsed:
            entries.append(parsed)

    row = {"session_uuid": SESSION_UUID, "tmux_name": TMUX_NAME}
    ts = _TailState()
    SessionMonitor._persist_turn_corrections(row, ts, entries)

    user = next(e for e in entries if e.get("type") == "user")
    assert user["message_id"].startswith("codex-user:")
    stored = dashboard_db.get_turn_correction(SESSION_UUID, user["message_id"])
    assert stored is not None
    assert stored["original_sha256"] == _sha("Jason encoded")
    assert stored["corrected_text"] == "JSON encoded"


def test_session_monitor_replay_does_not_mutate_terminal(test_app):
    """Replaying a history correction over an already-accepted row leaves it alone."""
    from tools.dashboard.session_monitor import SessionMonitor, _TailState

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
    SessionMonitor._persist_turn_corrections(row, _TailState(), [
        {"type": "user", "content": "text", "message_id": "msg-1"},
        {"type": "turn_correction", "corrected_text": "A"},
    ])
    stored = dashboard_db.get_turn_correction(SESSION_UUID, "msg-1")
    assert stored["status"] == "accepted"


# ── Accept → graph supersedes persistence (auto-edec1.6) ─────


def _install_persist_capture(monkeypatch):
    """Capture every ``persist_corrected_thought`` call without touching graph DB.

    The accept handler runs persistence in a thread via
    ``asyncio.to_thread``; the dashboard server imports
    ``tools.graph.ops`` as ``graph_ops`` at module load, so patching
    ``server.graph_ops.persist_corrected_thought`` is what actually
    intercepts the call. Returns a list the test can assert against.
    """
    captured: list[dict] = []

    def fake_persist(**kwargs):
        captured.append(kwargs)
        return {
            "thought_id": "fake-thought",
            "edge_id": "fake-edge",
            "source_id": "fake-source",
            "message_id": f"supersedes:{kwargs['target_message_id']}",
            "created": True,
        }

    from tools.dashboard import server as _server
    monkeypatch.setattr(
        _server.graph_ops, "persist_corrected_thought", fake_persist,
    )
    return captured


def _stub_workspace_resolver(monkeypatch, *, workspace_id="ws-test",
                             graph_project="autonomy"):
    """Pin _resolve_session_workspace so accept tests don't need real Settings.

    ``_resolve_session_workspace`` walks ``agents.workspace_settings.load_workspaces``
    which would otherwise hit shipped Settings or fail under the bare test
    harness. Tests stub it directly so the surface under test is the persist
    hook, not workspace registry plumbing.
    """
    from tools.dashboard import server as _server
    monkeypatch.setattr(
        _server, "_resolve_session_workspace",
        lambda sid, suuid: (workspace_id, graph_project),
    )


def _stub_setting(monkeypatch, *, persist_accepts: bool,
                  workspace_id="ws-test"):
    """Pretend ``autonomy.workspace.turn_correction#1`` resolves a fixed payload.

    Patches ``server.graph_ops.read_set`` to return one member with our chosen
    ``persist_accepts_to_graph`` value, keyed by ``workspace_id``.
    """
    from tools.dashboard import server as _server

    class _Member:
        def __init__(self, key, payload):
            self.key = key
            self.payload = payload
            self.org = "autonomy"

    class _Members:
        def __init__(self, members):
            self.members = members

    def fake_read_set(set_id, *, org=None, peers=None, target_revision=None):
        return _Members([_Member(
            workspace_id,
            {"persist_accepts_to_graph": persist_accepts},
        )])

    monkeypatch.setattr(_server.graph_ops, "read_set", fake_read_set)


def test_api_accept_with_persistence_setting_calls_graph(
    test_app, client, monkeypatch,
):
    """Setting ``persist_accepts_to_graph=true`` triggers graph persistence."""
    sha = _sha("Jason encoded")
    dashboard_db.upsert_turn_correction(
        SESSION_UUID, "msg-1",
        original_sha256=sha, corrected_text="JSON encoded",
        mode="balanced", reason="dictation cleanup", confidence=0.9,
    )
    captured = _install_persist_capture(monkeypatch)
    _stub_workspace_resolver(monkeypatch)
    _stub_setting(monkeypatch, persist_accepts=True)

    r = client.post(
        f"/api/session/{TMUX_NAME}/turn-corrections/msg-1/accept",
        json={"original_sha256": sha},
    )
    assert r.status_code == 200
    assert len(captured) == 1
    call = captured[0]
    assert call["org"] == "autonomy"
    assert call["session_uuid"] == SESSION_UUID
    assert call["target_message_id"] == "msg-1"
    assert call["original_sha256"] == sha
    assert call["corrected_text"] == "JSON encoded"
    extra = call.get("extra_metadata") or {}
    assert extra.get("mode") == "balanced"
    assert extra.get("reason") == "dictation cleanup"
    assert extra.get("confidence") == pytest.approx(0.9)


def test_api_accept_without_persistence_setting_skips_graph(
    test_app, client, monkeypatch,
):
    """Default Setting (``persist_accepts_to_graph=false``) keeps accept dashboard-only."""
    sha = _sha("Jason encoded")
    dashboard_db.upsert_turn_correction(
        SESSION_UUID, "msg-1",
        original_sha256=sha, corrected_text="JSON encoded",
    )
    captured = _install_persist_capture(monkeypatch)
    _stub_workspace_resolver(monkeypatch)
    _stub_setting(monkeypatch, persist_accepts=False)

    r = client.post(
        f"/api/session/{TMUX_NAME}/turn-corrections/msg-1/accept",
        json={"original_sha256": sha},
    )
    assert r.status_code == 200
    assert captured == []


def test_api_dismiss_never_calls_graph_persistence(
    test_app, client, monkeypatch,
):
    """Dismiss is never mirrored to graph, even with persistence enabled."""
    sha = _sha("Jason encoded")
    dashboard_db.upsert_turn_correction(
        SESSION_UUID, "msg-1",
        original_sha256=sha, corrected_text="JSON encoded",
    )
    captured = _install_persist_capture(monkeypatch)
    _stub_workspace_resolver(monkeypatch)
    _stub_setting(monkeypatch, persist_accepts=True)

    r = client.post(
        f"/api/session/{TMUX_NAME}/turn-corrections/msg-1/dismiss",
        json={"original_sha256": sha},
    )
    assert r.status_code == 200
    assert captured == []


def test_api_accept_skips_graph_when_workspace_unresolved(
    test_app, client, monkeypatch,
):
    """Host/path-derived sessions with no workspace mapping fail closed."""
    sha = _sha("Jason encoded")
    dashboard_db.upsert_turn_correction(
        SESSION_UUID, "msg-1",
        original_sha256=sha, corrected_text="JSON encoded",
    )
    captured = _install_persist_capture(monkeypatch)
    from tools.dashboard import server as _server
    monkeypatch.setattr(
        _server, "_resolve_session_workspace", lambda sid, suuid: None,
    )

    r = client.post(
        f"/api/session/{TMUX_NAME}/turn-corrections/msg-1/accept",
        json={"original_sha256": sha},
    )
    assert r.status_code == 200
    assert captured == []


def test_api_accept_swallows_graph_exception(
    test_app, client, monkeypatch,
):
    """Graph persistence failure must not break the accept response."""
    sha = _sha("Jason encoded")
    dashboard_db.upsert_turn_correction(
        SESSION_UUID, "msg-1",
        original_sha256=sha, corrected_text="JSON encoded",
    )
    _stub_workspace_resolver(monkeypatch)
    _stub_setting(monkeypatch, persist_accepts=True)

    from tools.dashboard import server as _server

    def fake_persist(**kwargs):
        raise RuntimeError("graph DB exploded")

    monkeypatch.setattr(
        _server.graph_ops, "persist_corrected_thought", fake_persist,
    )

    r = client.post(
        f"/api/session/{TMUX_NAME}/turn-corrections/msg-1/accept",
        json={"original_sha256": sha},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["correction"]["status"] == "accepted"
    # Dashboard accept stays committed even though graph mirror failed.
    stored = dashboard_db.get_turn_correction(SESSION_UUID, "msg-1")
    assert stored["status"] == "accepted"


def test_api_accept_idempotent_does_not_double_persist(
    test_app, client, monkeypatch,
):
    """Repeated accept POSTs cannot trigger duplicate graph writes.

    The second accept hits the dashboard's ``already_terminal`` short-circuit
    (409) before reaching the persistence hook — proves the dashboard layer
    itself guards against duplicate graph writes from a flaky operator click.
    """
    sha = _sha("Jason encoded")
    dashboard_db.upsert_turn_correction(
        SESSION_UUID, "msg-1",
        original_sha256=sha, corrected_text="JSON encoded",
    )
    captured = _install_persist_capture(monkeypatch)
    _stub_workspace_resolver(monkeypatch)
    _stub_setting(monkeypatch, persist_accepts=True)

    r1 = client.post(
        f"/api/session/{TMUX_NAME}/turn-corrections/msg-1/accept",
        json={"original_sha256": sha},
    )
    assert r1.status_code == 200
    r2 = client.post(
        f"/api/session/{TMUX_NAME}/turn-corrections/msg-1/accept",
        json={"original_sha256": sha},
    )
    assert r2.status_code == 409
    assert len(captured) == 1


def test_resolve_session_workspace_uses_session_project(test_app, monkeypatch):
    """Direct ``project`` → ``workspace.id`` lookup wins when populated."""
    from tools.dashboard import server as _server
    from agents import workspace_settings as _ws

    captured_lookup: dict[str, str] = {}

    class _FakeWS:
        def __init__(self, wid, gp):
            self.id = wid
            self.graph_project = gp

    def fake_get(workspace_id):
        captured_lookup["wid"] = workspace_id
        if workspace_id == "autonomy":
            return _FakeWS("autonomy", "autonomy")
        raise KeyError(workspace_id)

    monkeypatch.setattr(_ws, "get_workspace", fake_get)
    out = _server._resolve_session_workspace(TMUX_NAME, SESSION_UUID)
    assert out == ("autonomy", "autonomy")
    assert captured_lookup["wid"] == "autonomy"


def test_resolve_session_workspace_unresolvable_returns_none(
    test_app, monkeypatch,
):
    """Unknown ``project`` + no matching graph_project → None.

    Drives the fail-closed path the accept hook depends on: an unresolvable
    session must yield ``None`` so the caller can skip persistence rather
    than guessing an org or workspace.
    """
    from tools.dashboard import server as _server
    from agents import workspace_settings as _ws

    def fake_get(workspace_id):
        raise KeyError(workspace_id)

    def fake_load():
        return {}

    monkeypatch.setattr(_ws, "get_workspace", fake_get)
    monkeypatch.setattr(_ws, "load_workspaces", fake_load)
    out = _server._resolve_session_workspace(TMUX_NAME, SESSION_UUID)
    assert out is None
