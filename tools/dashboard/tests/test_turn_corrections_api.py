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
import logging
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




# ══════════════════════════════════════════════════════════════════
# auto-hmow2: authenticated suggest endpoint + live event delivery
# ══════════════════════════════════════════════════════════════════

SUGGEST_URL = "/api/session/turn-corrections/suggest"
_BEARER = {"Authorization": "Bearer test-token"}


def _auth_as(monkeypatch, session=TMUX_NAME, org="autonomy"):
    """Stub the shared session-auth helper's token resolution.

    Identity is whatever the bearer resolves to — the endpoint must derive the
    session from this, never from the URL or body.
    """
    from tools.dashboard import server as _server
    monkeypatch.setattr(_server.auth_db, "resolve_token", lambda _h: (session, org))


def _seed_user_jsonl(tmp_path, content, *, mid="u-target", session=TMUX_NAME, name="c.jsonl"):
    """Point ``session``'s jsonl_path at a controlled Claude user turn."""
    p = tmp_path / "sessions" / session / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "type": "user", "uuid": mid,
        "message": {"role": "user", "content": [{"type": "text", "text": content}]},
        "timestamp": "2026-08-10T12:00:00Z",
    }) + "\n")
    conn = dashboard_db.get_conn()
    conn.execute(
        "UPDATE tmux_sessions SET jsonl_path=? WHERE tmux_name=?", (str(p), session))
    conn.commit()
    return mid


def _capture_broadcasts(monkeypatch):
    """Record every event_bus.broadcast call; note DB row state at fire time."""
    from tools.dashboard import server as _server
    calls = []

    async def rec(topic, data, dedup=True):
        row_at_fire = None
        if topic == "session:turn_corrections":
            corr = (data or {}).get("correction") or {}
            row_at_fire = dashboard_db.get_turn_correction(
                (data or {}).get("session_uuid"), corr.get("target_message_id"))
        calls.append({"topic": topic, "data": data, "dedup": dedup,
                      "row_committed_at_fire": row_at_fire})
        return 0

    monkeypatch.setattr(_server.event_bus, "broadcast", rec)
    return calls


def _tc_broadcasts(calls):
    return [c for c in calls if c["topic"] == "session:turn_corrections"]


# ── happy path ─────────────────────────────────────────────────


def test_suggest_creates_pending_and_broadcasts(test_app, client, monkeypatch, tmp_path):
    _auth_as(monkeypatch)
    mid = _seed_user_jsonl(tmp_path, "Plese reviw the corections API")
    calls = _capture_broadcasts(monkeypatch)

    r = client.post(SUGGEST_URL, headers=_BEARER,
                    json={"corrected_text": "Please review the corrections API"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["session_id"] == TMUX_NAME
    corr = body["correction"]
    assert corr["status"] == "pending"
    assert corr["target_message_id"] == mid
    assert corr["corrected_text"] == "Please review the corrections API"
    assert corr["original_sha256"] == _sha("Plese reviw the corections API")

    # Row committed exactly once.
    stored = dashboard_db.get_turn_correction(SESSION_UUID, mid)
    assert stored is not None and stored["status"] == "pending"

    # Exactly one turn-correction broadcast, keyed by canonical tmux name,
    # carrying the exact committed row — and the row already existed in the DB
    # when the broadcast fired (persist first, broadcast second).
    tc_calls = _tc_broadcasts(calls)
    assert len(tc_calls) == 1
    call = tc_calls[0]
    assert call["data"]["session_id"] == TMUX_NAME
    assert call["data"]["session_uuid"] == SESSION_UUID
    assert call["data"]["correction"] == corr
    assert call["dedup"] is False
    assert call["row_committed_at_fire"] is not None


def test_suggest_body_verbatim_long_multiline(test_app, client, monkeypatch, tmp_path):
    _auth_as(monkeypatch)
    body_para = "This is a substantial paragraph of dictated prose that needs a light cleanup. " * 6
    raw = "here is a long messege\n\n" + body_para + "\n\nand it ends with a finl line"
    _seed_user_jsonl(tmp_path, raw)
    corrected = "Here is a long message.\n\n" + body_para + "\n\nAnd it ends with a final line."
    assert "\n\n" in corrected and len(corrected) > 400
    r = client.post(SUGGEST_URL, headers=_BEARER, json={"corrected_text": corrected})
    assert r.status_code == 201, r.text
    # Multiline body preserved verbatim in the committed row.
    assert r.json()["correction"]["corrected_text"] == corrected


def test_suggest_metadata_transmitted(test_app, client, monkeypatch, tmp_path):
    _auth_as(monkeypatch)
    _seed_user_jsonl(tmp_path, "Plese reviw the corections API")
    r = client.post(SUGGEST_URL, headers=_BEARER, json={
        "corrected_text": "Please review the corrections API",
        "mode": "aggressive", "reason": "dictation", "confidence": 0.98,
    })
    assert r.status_code == 201, r.text
    corr = r.json()["correction"]
    assert corr["mode"] == "aggressive"
    assert corr["reason"] == "dictation"
    assert corr["confidence"] == pytest.approx(0.98)


# ── identity is token-derived, never caller-controlled ─────────


@pytest.mark.parametrize("field,value", [
    ("session_id", "auto-someone-else"),
    ("session_uuid", "uuid-someone-else"),
    ("target_message_id", "m-forged"),
    ("original_sha256", "deadbeef"),
])
def test_suggest_rejects_body_identity_fields(test_app, client, monkeypatch, tmp_path, field, value):
    _auth_as(monkeypatch)
    _seed_user_jsonl(tmp_path, "Plese reviw the corections API")
    calls = _capture_broadcasts(monkeypatch)
    r = client.post(SUGGEST_URL, headers=_BEARER, json={
        "corrected_text": "Please review the corrections API", field: value,
    })
    assert r.status_code == 400
    assert field in r.json()["error"]
    assert _tc_broadcasts(calls) == []


def test_suggest_session_is_token_derived_not_body(test_app, client, monkeypatch, tmp_path):
    """The committed row lands on the token's session even though nothing in the
    body names a session — proving a caller cannot target another session."""
    _auth_as(monkeypatch, session=TMUX_NAME)
    mid = _seed_user_jsonl(tmp_path, "Plese reviw the corections API")
    r = client.post(SUGGEST_URL, headers=_BEARER,
                    json={"corrected_text": "Please review the corrections API"})
    assert r.status_code == 201, r.text
    # Landed on the token session's uuid, nowhere else.
    assert dashboard_db.get_turn_correction(SESSION_UUID, mid) is not None
    assert r.json()["session_id"] == TMUX_NAME


# ── auth failures ──────────────────────────────────────────────


def test_suggest_missing_bearer_401(test_app, client, monkeypatch, tmp_path):
    calls = _capture_broadcasts(monkeypatch)
    r = client.post(SUGGEST_URL, json={"corrected_text": "x"})
    assert r.status_code == 401
    assert _tc_broadcasts(calls) == []


def test_suggest_invalid_token_401(test_app, client, monkeypatch, tmp_path):
    from tools.dashboard import server as _server
    monkeypatch.setattr(_server.auth_db, "resolve_token", lambda _h: None)
    calls = _capture_broadcasts(monkeypatch)
    r = client.post(SUGGEST_URL, headers=_BEARER, json={"corrected_text": "x"})
    assert r.status_code == 401
    assert _tc_broadcasts(calls) == []


# ── session-state failures ─────────────────────────────────────


def test_suggest_unknown_session_404(test_app, client, monkeypatch):
    _auth_as(monkeypatch, session="auto-ghost-session")
    calls = _capture_broadcasts(monkeypatch)
    r = client.post(SUGGEST_URL, headers=_BEARER, json={"corrected_text": "x"})
    assert r.status_code == 404
    assert _tc_broadcasts(calls) == []


def test_suggest_unlinked_session_409(test_app, client, monkeypatch):
    # auto-test-validator exists but has no jsonl_path / session_uuid link.
    _auth_as(monkeypatch, session="auto-test-validator")
    conn = dashboard_db.get_conn()
    conn.execute("UPDATE tmux_sessions SET jsonl_path=NULL, session_uuid=NULL"
                 " WHERE tmux_name=?", ("auto-test-validator",))
    conn.commit()
    calls = _capture_broadcasts(monkeypatch)
    r = client.post(SUGGEST_URL, headers=_BEARER, json={"corrected_text": "x"})
    assert r.status_code == 409
    assert _tc_broadcasts(calls) == []


def test_suggest_no_target_409(test_app, client, monkeypatch, tmp_path):
    _auth_as(monkeypatch)
    _seed_user_jsonl(tmp_path, "Proceed")
    calls = _capture_broadcasts(monkeypatch)
    r = client.post(SUGGEST_URL, headers=_BEARER, json={
        "corrected_text": (
            "A wholly unrelated multi-sentence replacement sharing no vocabulary "
            "with the single short user turn on record."
        ),
    })
    assert r.status_code == 409
    assert dashboard_db.list_turn_corrections(SESSION_UUID) == []
    assert _tc_broadcasts(calls) == []


def test_suggest_persistence_failure_500_no_broadcast(test_app, client, monkeypatch, tmp_path):
    _auth_as(monkeypatch)
    _seed_user_jsonl(tmp_path, "Plese reviw the corections API")
    from tools.dashboard import server as _server

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(_server.dashboard_db, "upsert_turn_correction", boom)
    calls = _capture_broadcasts(monkeypatch)
    r = client.post(SUGGEST_URL, headers=_BEARER,
                    json={"corrected_text": "Please review the corrections API"})
    assert r.status_code == 500
    assert _tc_broadcasts(calls) == []


# ── body validation ────────────────────────────────────────────


def test_suggest_invalid_json_400(test_app, client, monkeypatch):
    _auth_as(monkeypatch)
    r = client.post(SUGGEST_URL, headers={**_BEARER, "Content-Type": "application/json"},
                    content=b"{not json")
    assert r.status_code == 400


def test_suggest_missing_corrected_text_400(test_app, client, monkeypatch):
    _auth_as(monkeypatch)
    r = client.post(SUGGEST_URL, headers=_BEARER, json={"mode": "balanced"})
    assert r.status_code == 400


def test_suggest_empty_corrected_text_400(test_app, client, monkeypatch):
    _auth_as(monkeypatch)
    r = client.post(SUGGEST_URL, headers=_BEARER, json={"corrected_text": ""})
    assert r.status_code == 400


def test_suggest_invalid_mode_400(test_app, client, monkeypatch):
    _auth_as(monkeypatch)
    r = client.post(SUGGEST_URL, headers=_BEARER,
                    json={"corrected_text": "x", "mode": "wild"})
    assert r.status_code == 400


@pytest.mark.parametrize("bad", [-0.1, 1.5, "high", True])
def test_suggest_bad_confidence_400(test_app, client, monkeypatch, bad):
    _auth_as(monkeypatch)
    r = client.post(SUGGEST_URL, headers=_BEARER,
                    json={"corrected_text": "x", "confidence": bad})
    assert r.status_code == 400


# ── idempotent pending upsert ──────────────────────────────────


def test_suggest_repeat_refreshes_same_pending_row(test_app, client, monkeypatch, tmp_path):
    _auth_as(monkeypatch)
    mid = _seed_user_jsonl(tmp_path, "Plese reviw the corections API")
    body = {"corrected_text": "Please review the corrections API"}
    r1 = client.post(SUGGEST_URL, headers=_BEARER, json=body)
    r2 = client.post(SUGGEST_URL, headers=_BEARER, json=body)
    assert r1.status_code == 201 and r2.status_code == 201
    # One row for the target, still pending.
    rows = dashboard_db.list_turn_corrections(SESSION_UUID)
    assert len([x for x in rows if x["target_message_id"] == mid]) == 1


def test_suggest_terminal_target_not_reopened_409(test_app, client, monkeypatch, tmp_path):
    """A target already accepted must not be reopened by a new suggestion."""
    _auth_as(monkeypatch)
    mid = _seed_user_jsonl(tmp_path, "Plese reviw the corections API")
    sha = _sha("Plese reviw the corections API")
    dashboard_db.upsert_turn_correction(
        SESSION_UUID, mid, original_sha256=sha, corrected_text="accepted already")
    dashboard_db.set_turn_correction_status(SESSION_UUID, mid, "accepted", expected_sha256=sha)
    calls = _capture_broadcasts(monkeypatch)
    r = client.post(SUGGEST_URL, headers=_BEARER,
                    json={"corrected_text": "Please review the corrections API"})
    # Only one user turn exists and it is terminal → no acceptable target.
    assert r.status_code == 409
    assert _tc_broadcasts(calls) == []
    # Terminal row is untouched.
    assert dashboard_db.get_turn_correction(SESSION_UUID, mid)["status"] == "accepted"


# ── accept/dismiss broadcast + graph independence ──────────────


def test_accept_broadcasts_terminal_row_despite_graph_failure(test_app, client, monkeypatch):
    sha = _sha("raw text")
    dashboard_db.upsert_turn_correction(
        SESSION_UUID, "msg-acc", original_sha256=sha, corrected_text="fixed text")
    _stub_workspace_resolver(monkeypatch)
    _stub_setting(monkeypatch, persist_accepts=True)
    from tools.dashboard import server as _server
    monkeypatch.setattr(_server.graph_ops, "persist_corrected_thought",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("graph down")))
    calls = _capture_broadcasts(monkeypatch)
    r = client.post(f"/api/session/{TMUX_NAME}/turn-corrections/msg-acc/accept",
                    json={"original_sha256": sha})
    assert r.status_code == 200
    tc_calls = _tc_broadcasts(calls)
    assert len(tc_calls) == 1
    assert tc_calls[0]["data"]["correction"]["status"] == "accepted"


def test_dismiss_broadcasts_terminal_row(test_app, client, monkeypatch):
    sha = _sha("raw text 2")
    dashboard_db.upsert_turn_correction(
        SESSION_UUID, "msg-dis", original_sha256=sha, corrected_text="whatever")
    calls = _capture_broadcasts(monkeypatch)
    r = client.post(f"/api/session/{TMUX_NAME}/turn-corrections/msg-dis/dismiss",
                    json={"original_sha256": sha})
    assert r.status_code == 200
    tc_calls = _tc_broadcasts(calls)
    assert len(tc_calls) == 1
    assert tc_calls[0]["data"]["correction"]["status"] == "dismissed"


# ── event-bus fan-out: two subscribers, one committed row ──────


def test_event_bus_fans_out_committed_row_to_all_subscribers():
    from tools.dashboard.event_bus import EventBus
    bus = EventBus()
    q1 = bus.subscribe("viewer-1")
    q2 = bus.subscribe("viewer-2")
    row = {
        "session_uuid": SESSION_UUID, "target_message_id": "m1",
        "status": "pending", "original_sha256": "abc", "corrected_text": "fixed",
        "mode": None, "reason": None, "confidence": None,
        "created_at": 1.0, "updated_at": 1.0,
    }
    payload = {"session_id": TMUX_NAME, "session_uuid": SESSION_UUID, "correction": row}
    n = bus.broadcast_sync("session:turn_corrections", payload, dedup=False)
    assert n == 2
    got1 = q1.get_nowait()
    got2 = q2.get_nowait()
    assert got1[1]["correction"] == row
    assert got2[1]["correction"] == row


def test_event_bus_replay_same_row_is_delivered_each_time():
    """At-least-once delivery: the same committed row is delivered on replay
    (dedup=False), so consumers must tolerate duplicates."""
    from tools.dashboard.event_bus import EventBus
    bus = EventBus()
    q = bus.subscribe("viewer")
    row = {"target_message_id": "m1", "status": "pending", "corrected_text": "x"}
    payload = {"session_id": TMUX_NAME, "session_uuid": SESSION_UUID, "correction": row}
    bus.broadcast_sync("session:turn_corrections", payload, dedup=False)
    bus.broadcast_sync("session:turn_corrections", payload, dedup=False)
    assert q.get_nowait()[1]["correction"] == row
    assert q.get_nowait()[1]["correction"] == row
