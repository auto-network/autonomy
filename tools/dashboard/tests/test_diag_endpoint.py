"""Diag round-trip endpoint tests.

Covers ``GET /api/diag/sessions``, ``POST /api/diag/client``, and
``POST /api/diag/eventbus/snapshot`` — the three handlers introduced by
auto-zh75w that align file/server/bus/client view of session state.

Tests use Starlette's TestClient with a fresh server reload so the test
DB and diag aggregator state stay isolated from each other.
"""

import asyncio
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest


# ── Test fixtures ──────────────────────────────────────────────────────


def _init_test_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """CREATE TABLE IF NOT EXISTS tmux_sessions (
            tmux_name TEXT PRIMARY KEY, session_uuid TEXT,
            graph_source_id TEXT, type TEXT NOT NULL, project TEXT NOT NULL,
            jsonl_path TEXT, bead_id TEXT, created_at REAL NOT NULL,
            is_live INTEGER DEFAULT 1, file_offset INTEGER DEFAULT 0,
            last_activity REAL, last_message TEXT DEFAULT '',
            entry_count INTEGER DEFAULT 0, context_tokens INTEGER DEFAULT 0,
            label TEXT DEFAULT '', topics TEXT DEFAULT '[]',
            role TEXT DEFAULT '', nag_enabled INTEGER DEFAULT 0,
            nag_interval INTEGER DEFAULT 15, nag_message TEXT DEFAULT '',
            nag_last_sent REAL DEFAULT 0, dispatch_nag INTEGER DEFAULT 0,
            resolution_dir TEXT, session_uuids TEXT DEFAULT '[]',
            curr_jsonl_file TEXT
        )"""
    )
    conn.commit()
    conn.close()


def _insert_session(
    db_path: Path, tmux_name: str, jsonl_path: str | None,
    file_offset: int = 0,
) -> None:
    conn = sqlite3.connect(str(db_path))
    res_dir = str(Path(jsonl_path).parent) if jsonl_path else None
    conn.execute(
        "INSERT INTO tmux_sessions"
        " (tmux_name, type, project, jsonl_path, created_at, is_live,"
        "  resolution_dir, session_uuids, curr_jsonl_file, file_offset)"
        " VALUES (?, 'container', 'test', ?, ?, 1, ?, '[]', ?, ?)",
        (tmux_name, jsonl_path, time.time(), res_dir, jsonl_path, file_offset),
    )
    conn.commit()
    conn.close()


def _write_entries(jsonl_path: Path, entries: list[dict]) -> None:
    with open(jsonl_path, "a") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")


def _toolish_entries() -> list[dict]:
    """Produce three JSONL rows whose identity keys match what the server emits."""
    return [
        {"type": "tool_use", "tool_id": "tool_abc", "tool_name": "Read",
         "timestamp": "2026-04-28T04:31:25.111Z"},
        {"type": "tool_result", "tool_id": "tool_abc", "status": "completed",
         "timestamp": "2026-04-28T04:31:27.220Z"},
        {"type": "assistant_text", "content": "Let me check…",
         "timestamp": "2026-04-28T04:31:28.974Z"},
    ]


@pytest.fixture
def diag_env(tmp_path, monkeypatch):
    """Boot a fresh dashboard server bound to a tmp_path-scoped DB.

    The reload ensures _DIAG_AGGREGATORS resets between tests. The
    ``_check_tmux`` patch keeps the liveness loop from marking test sessions
    dead the moment the TestClient lifespan starts.
    """
    db_path = tmp_path / "dashboard.db"
    _init_test_db(db_path)
    monkeypatch.setenv("DASHBOARD_DB", str(db_path))
    monkeypatch.setenv("DASHBOARD_EVENT_BUS_STATE", str(tmp_path / "event_bus.state"))

    import importlib
    from tools.dashboard.dao import dashboard_db as db_mod
    importlib.reload(db_mod)
    from tools.dashboard import event_bus as event_bus_mod
    importlib.reload(event_bus_mod)
    from tools.dashboard import session_monitor as monitor_mod
    importlib.reload(monitor_mod)
    from tools.dashboard import server as server_mod
    importlib.reload(server_mod)

    # Pin server's diag dir under tmp_path so tests never write into the repo.
    server_mod._DIAG_DIR = tmp_path / "diag"

    patcher = patch.object(
        monitor_mod.SessionMonitor, "_check_tmux", staticmethod(lambda name: True),
    )
    patcher.start()
    try:
        yield server_mod, tmp_path, db_path
    finally:
        patcher.stop()


def _short_window(server_mod, monkeypatch=None):
    """Force the diag collection window to a short interval to keep tests fast."""
    server_mod._DIAG_COLLECTION_WINDOW_SECONDS = 0.3


# ── GET /api/diag/sessions — no clients ────────────────────────────────


class TestDiagSessionsBasics:
    def test_returns_json_with_no_clients(self, diag_env):
        server_mod, tmp_path, db_path = diag_env
        from starlette.testclient import TestClient

        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir()
        jsonl = sess_dir / "auto-1.jsonl"
        _write_entries(jsonl, _toolish_entries())
        _insert_session(db_path, "auto-1", str(jsonl), file_offset=jsonl.stat().st_size)

        _short_window(server_mod)
        with TestClient(server_mod.app) as client:
            t0 = time.monotonic()
            resp = client.get("/api/diag/sessions")
            elapsed = time.monotonic() - t0
            assert resp.status_code == 200
            assert elapsed < 3.1, f"diag took too long: {elapsed:.2f}s"
            body = resp.json()

        assert body["request_type"] == "session_markers"
        assert body["clients_responded"] == 0
        # Bus block fields
        bus = body["bus"]
        for key in [
            "global_seq", "epoch", "epoch_age_s", "subscribers_count",
            "buffer_entries", "buffer_bytes", "buffer_first_seq",
            "buffer_last_seq", "buffer_first_ts", "buffer_last_ts",
            "broadcasts_last_60s", "last_snapshot_path", "last_snapshot_mtime",
        ]:
            assert key in bus, f"missing bus.{key}"
        # The diag broadcast itself counts.
        assert bus["broadcasts_last_60s"] >= 1
        # Per-row shape
        rows = body["rows"]
        assert any(r["session_id"] == "auto-1" for r in rows)
        row = next(r for r in rows if r["session_id"] == "auto-1")
        assert row["clients"] == []
        assert row["max_client_lag_ms"] is None
        assert row["min_client_seq"] is None
        # File layer
        assert row["file"]["lines"] == 3
        assert row["file"]["path_resolved"] == str(jsonl.resolve())
        assert row["file"]["inode"] is not None
        assert row["file"]["device"] is not None
        assert row["file"]["mtime_ago_s"] is not None
        assert len(row["file"]["tail_3"]) == 3
        # Server layer present (may be empty until tail runs, but block exists)
        assert "broadcast_seq" in row["server"]
        assert "tail_3" in row["server"]
        # Drift block
        assert row["drift"]["tail_3_alignment"] in (
            "all_match", "file_server_match_clients_diverge",
            "file_diverges", "clients_disagree",
        )

    def test_session_filter(self, diag_env):
        server_mod, tmp_path, db_path = diag_env
        from starlette.testclient import TestClient

        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir()
        for sid in ("auto-1", "auto-2"):
            jsonl = sess_dir / f"{sid}.jsonl"
            _write_entries(jsonl, _toolish_entries())
            _insert_session(db_path, sid, str(jsonl), file_offset=jsonl.stat().st_size)

        _short_window(server_mod)
        with TestClient(server_mod.app) as client:
            resp = client.get("/api/diag/sessions?session=auto-2")
            body = resp.json()
        assert resp.status_code == 200
        assert [r["session_id"] for r in body["rows"]] == ["auto-2"]

    def test_text_format_table(self, diag_env):
        server_mod, tmp_path, db_path = diag_env
        from starlette.testclient import TestClient

        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir()
        jsonl = sess_dir / "auto-1.jsonl"
        _write_entries(jsonl, _toolish_entries())
        _insert_session(db_path, "auto-1", str(jsonl), file_offset=jsonl.stat().st_size)

        _short_window(server_mod)
        with TestClient(server_mod.app) as client:
            resp = client.get("/api/diag/sessions?format=text")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]
        text = resp.text
        # Header + at least one data row
        assert "session_id" in text
        assert "align" in text
        assert "auto-1" in text


# ── POST /api/diag/client — aggregator routing ─────────────────────────


class TestDiagClientPost:
    def test_unknown_req_id_returns_404(self, diag_env):
        server_mod, _tmp, _db = diag_env
        from starlette.testclient import TestClient
        with TestClient(server_mod.app) as client:
            resp = client.post(
                "/api/diag/client",
                json={
                    "req_id": "00000000-0000-0000-0000-000000000000",
                    "request_type": "session_markers",
                    "client_id": "test-client",
                    "payload": {"sessions": {}, "client_state": {}},
                },
            )
        assert resp.status_code == 404
        assert "error" in resp.json()

    def test_request_type_mismatch_returns_400(self, diag_env):
        server_mod, _tmp, _db = diag_env

        # Inject an aggregator manually so we can craft a mismatch without
        # racing the GET window.
        req_id = "11111111-1111-1111-1111-111111111111"
        server_mod._DIAG_AGGREGATORS[req_id] = {
            "emit_ts": time.time(),
            "request_type": "session_markers",
            "deadline_ts": time.time() + 5,
            "params": {"sessions": []},
            "clients": {},
        }

        from starlette.testclient import TestClient
        with TestClient(server_mod.app) as client:
            resp = client.post(
                "/api/diag/client",
                json={
                    "req_id": req_id,
                    "request_type": "different_type",
                    "client_id": "test-client",
                    "payload": {},
                },
            )
        assert resp.status_code == 400
        assert "error" in resp.json()
        # Must NOT have stashed the bad reply
        assert server_mod._DIAG_AGGREGATORS[req_id]["clients"] == {}

    def test_missing_envelope_fields_returns_400(self, diag_env):
        server_mod, _tmp, _db = diag_env
        from starlette.testclient import TestClient
        with TestClient(server_mod.app) as client:
            resp = client.post(
                "/api/diag/client",
                json={"req_id": "abc", "request_type": "session_markers"},
            )
        assert resp.status_code == 400


# ── End-to-end with a fake client ─────────────────────────────────────


class TestDiagWithFakeClient:
    def test_fake_client_appears_in_aggregate(self, diag_env):
        """Spawn a thread that POSTs to /api/diag/client mid-window."""
        server_mod, tmp_path, db_path = diag_env
        from starlette.testclient import TestClient
        from tools.dashboard import session_monitor as monitor_mod

        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir()
        jsonl = sess_dir / "auto-1.jsonl"
        _write_entries(jsonl, _toolish_entries())
        _insert_session(db_path, "auto-1", str(jsonl), file_offset=jsonl.stat().st_size)

        # Pre-seed the server-side tail so file/server share the same identity
        # keys — the steady-state we want to verify reports as "all_match".
        ts = monitor_mod._TailState()
        for entry in _toolish_entries():
            ts.recent_processed.append((
                entry.get("type", ""),
                entry.get("timestamp", ""),
                monitor_mod._entry_identity(entry),
            ))
        ts.broadcast_seq = 3
        monitor_mod.session_monitor._tail_states["auto-1"] = ts

        _short_window(server_mod)
        # Make the window long enough for the helper thread to win the race
        # but short enough to keep the test fast.
        server_mod._DIAG_COLLECTION_WINDOW_SECONDS = 1.0

        with TestClient(server_mod.app) as client:
            tail3 = [
                {"type": "tool_use", "timestamp": "2026-04-28T04:31:25.111Z",
                 "identity": "tu:tool_abc"},
                {"type": "tool_result", "timestamp": "2026-04-28T04:31:27.220Z",
                 "identity": "tr:tool_abc"},
                {"type": "assistant_text",
                 "timestamp": "2026-04-28T04:31:28.974Z",
                 "identity": "assistant_text:2026-04-28T04:31:28.974Z:Let me check…"},
            ]

            def _post_when_aggregator_ready():
                deadline = time.time() + 2.0
                while time.time() < deadline:
                    if server_mod._DIAG_AGGREGATORS:
                        req_id = next(iter(server_mod._DIAG_AGGREGATORS))
                        body = {
                            "req_id": req_id,
                            "request_type": "session_markers",
                            "client_id": "fake-client-A",
                            "payload": {
                                "client_state": {
                                    "event_source_ready_state": 1,
                                    "replaying": False,
                                    "held_events_count": 0,
                                    "client_last_seq": 99,
                                    "server_epoch": 1745859600,
                                },
                                "sessions": {
                                    "auto-1": {
                                        "store_seq": 3,
                                        "tile_count": 3,
                                        "store_loading": False,
                                        "pending_sse_count": 0,
                                        "is_focused_viewer": True,
                                        "last_activity_ms": 100,
                                        "last_topic_seq": 99,
                                        "store_first_entry_ts": "",
                                        "store_last_entry_ts": "",
                                        "out_of_order_count": 0,
                                        "idle_ms": 100,
                                        "tail_3": tail3,
                                    },
                                },
                            },
                        }
                        client.post("/api/diag/client", json=body)
                        return
                    time.sleep(0.02)

            thread = threading.Thread(target=_post_when_aggregator_ready)
            thread.start()
            resp = client.get("/api/diag/sessions")
            thread.join(timeout=3.0)
            body = resp.json()

        assert body["clients_responded"] == 1
        row = next(r for r in body["rows"] if r["session_id"] == "auto-1")
        assert len(row["clients"]) == 1
        client_block = row["clients"][0]
        assert client_block["client_id"] == "fake-client-A"
        assert client_block["lag_ms"] is not None
        assert client_block["lag_ms"] >= 0
        # Three sub-blocks: top-level fields + client_state + session_markers
        assert "client_state" in client_block
        assert client_block["client_state"]["event_source_ready_state"] == 1
        assert "session_markers" in client_block
        assert client_block["session_markers"]["store_seq"] == 3
        assert len(client_block["session_markers"]["tail_3"]) == 3
        # File, server, and client tail_3 share identity keys → all_match.
        assert row["drift"]["tail_3_alignment"] == "all_match"

    def test_two_req_ids_dont_cross_contaminate(self, diag_env):
        server_mod, _tmp, _db = diag_env
        # Two synthetic aggregators with different req_ids
        rid_a = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        rid_b = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        server_mod._DIAG_AGGREGATORS[rid_a] = {
            "emit_ts": time.time(),
            "request_type": "session_markers",
            "deadline_ts": time.time() + 5,
            "params": {"sessions": ["auto-1"]},
            "clients": {},
        }
        server_mod._DIAG_AGGREGATORS[rid_b] = {
            "emit_ts": time.time(),
            "request_type": "session_markers",
            "deadline_ts": time.time() + 5,
            "params": {"sessions": ["auto-1"]},
            "clients": {},
        }

        from starlette.testclient import TestClient
        with TestClient(server_mod.app) as client:
            client.post(
                "/api/diag/client",
                json={
                    "req_id": rid_a, "request_type": "session_markers",
                    "client_id": "tab-A",
                    "payload": {"sessions": {}, "client_state": {}},
                },
            )
            client.post(
                "/api/diag/client",
                json={
                    "req_id": rid_b, "request_type": "session_markers",
                    "client_id": "tab-B",
                    "payload": {"sessions": {}, "client_state": {}},
                },
            )

        assert "tab-A" in server_mod._DIAG_AGGREGATORS[rid_a]["clients"]
        assert "tab-B" in server_mod._DIAG_AGGREGATORS[rid_b]["clients"]
        assert "tab-B" not in server_mod._DIAG_AGGREGATORS[rid_a]["clients"]
        assert "tab-A" not in server_mod._DIAG_AGGREGATORS[rid_b]["clients"]


# ── Drift detection ────────────────────────────────────────────────────


class TestDiagDriftDetection:
    def test_corrupt_server_tail_flips_alignment(self, diag_env):
        """Force a divergence between file tail and server tail and confirm the diag
        response no longer reports `all_match`."""
        server_mod, tmp_path, db_path = diag_env
        from starlette.testclient import TestClient
        from tools.dashboard import session_monitor as monitor_mod

        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir()
        jsonl = sess_dir / "auto-1.jsonl"
        _write_entries(jsonl, _toolish_entries())
        _insert_session(db_path, "auto-1", str(jsonl), file_offset=jsonl.stat().st_size)

        # Inject a divergent server-side tail_3 directly onto the _TailState.
        ts = monitor_mod._TailState()
        ts.recent_processed.append(
            ("assistant_text", "1990-01-01T00:00:00Z", "assistant_text:bogus")
        )
        monitor_mod.session_monitor._tail_states["auto-1"] = ts

        _short_window(server_mod)
        with TestClient(server_mod.app) as client:
            resp = client.get("/api/diag/sessions?session=auto-1")
            body = resp.json()
        assert resp.status_code == 200
        row = next(r for r in body["rows"] if r["session_id"] == "auto-1")
        # File tail_3 is real, server tail_3 is fake → file_diverges (no clients).
        assert row["drift"]["tail_3_alignment"] == "file_diverges"


# ── Bus-block correctness ─────────────────────────────────────────────


class TestDiagBusBlock:
    def test_subscribers_count_reflects_subscribers(self, diag_env):
        """bus.subscribers_count tracks live SSE connections."""
        server_mod, _tmp, _db = diag_env

        bus = server_mod.event_bus
        assert bus.subscribers_count() == 0
        q1 = bus.subscribe()
        q2 = bus.subscribe()
        try:
            assert bus.subscribers_count() == 2
        finally:
            bus.unsubscribe(q1)
            bus.unsubscribe(q2)
        assert bus.subscribers_count() == 0

    def test_buffer_window_matches_buffer(self, diag_env):
        """bus.buffer_window first/last seq align with the live ring buffer."""
        server_mod, _tmp, _db = diag_env

        bus = server_mod.event_bus
        # Empty buffer → all None.
        first_seq, last_seq, first_ts, last_ts = bus.buffer_window()
        assert (first_seq, last_seq, first_ts, last_ts) == (None, None, None, None)

        async def _populate():
            for i in range(5):
                await bus.broadcast("test:diag", {"i": i}, dedup=False)
        asyncio.run(_populate())

        first_seq, last_seq, first_ts, last_ts = bus.buffer_window()
        assert first_seq == bus._buffer[0].seq
        assert last_seq == bus._buffer[-1].seq
        assert first_ts is not None and last_ts is not None
        assert last_ts >= first_ts


# ── EventBus snapshot endpoint ────────────────────────────────────────


_SNAPSHOT_FILENAME_RE = re.compile(r"^eventbus-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{6}Z\.state$")


class TestDiagEventBusSnapshot:
    def test_snapshot_writes_under_data_diag(self, diag_env):
        server_mod, tmp_path, _db = diag_env
        from starlette.testclient import TestClient

        # Populate the bus so the snapshot has something to round-trip.
        async def _populate():
            for i in range(3):
                await server_mod.event_bus.broadcast(
                    "test:snapshot", {"i": i}, dedup=False,
                )
        asyncio.run(_populate())

        with TestClient(server_mod.app) as client:
            resp = client.post("/api/diag/eventbus/snapshot")
        assert resp.status_code == 200
        body = resp.json()
        for key in ("path", "bytes", "seq", "epoch", "buffer_entries", "buffer_bytes"):
            assert key in body, f"missing {key}"
        path = Path(body["path"])
        assert path.exists()
        # Lives under tmp_path/diag/, not the real repo data/.
        assert str(path.parent) == str(tmp_path / "diag")
        assert _SNAPSHOT_FILENAME_RE.match(path.name), path.name
        # Roundtrip via restore on a fresh bus.
        from tools.dashboard.event_bus import EventBus
        new_bus = EventBus()
        assert new_bus.restore(path) is True
        assert new_bus._seq == server_mod.event_bus._seq
        assert "test:snapshot" in new_bus._last

    def test_snapshot_does_not_overwrite_event_bus_state(self, diag_env):
        server_mod, tmp_path, _db = diag_env
        from starlette.testclient import TestClient

        with TestClient(server_mod.app) as client:
            resp1 = client.post("/api/diag/eventbus/snapshot")
            # Sleep to ensure microsecond timestamp differs.
            time.sleep(0.01)
            resp2 = client.post("/api/diag/eventbus/snapshot")
        assert resp1.status_code == 200
        assert resp2.status_code == 200
        path1 = Path(resp1.json()["path"])
        path2 = Path(resp2.json()["path"])
        assert path1 != path2
        # Both files exist.
        assert path1.exists() and path2.exists()
        # Neither path is the live event_bus.state.
        assert path1.name != "event_bus.state"
        assert path2.name != "event_bus.state"

    def test_snapshot_does_not_accept_caller_path(self, diag_env):
        """The snapshot endpoint ignores any client-supplied path/body fields."""
        server_mod, tmp_path, _db = diag_env
        from starlette.testclient import TestClient

        with TestClient(server_mod.app) as client:
            # Even if a client tries to pass a path, the response writes to
            # data/diag/ as required by the spec.
            resp = client.post(
                "/api/diag/eventbus/snapshot",
                json={"path": "/tmp/evil-path.state"},
            )
        assert resp.status_code == 200
        body = resp.json()
        out_path = Path(body["path"])
        assert "/tmp/evil-path.state" != str(out_path)
        assert str(out_path).startswith(str(tmp_path / "diag"))


# ── auto-wldnv: maximum-information enrichment ───────────────────────────


def _post_diag_reply(server_mod, client, sid, *, sessions_payload=None,
                     client_state=None, client_id="tab-X"):
    """Helper: POST a synthetic diag reply mid-window. Returns (body, req_id)."""
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if server_mod._DIAG_AGGREGATORS:
            req_id = next(iter(server_mod._DIAG_AGGREGATORS))
            payload = {
                "client_state": client_state or {},
                "sessions": sessions_payload or {},
            }
            body = {
                "req_id": req_id,
                "request_type": "session_markers",
                "client_id": client_id,
                "payload": payload,
            }
            client.post("/api/diag/client", json=body)
            return body, req_id
        time.sleep(0.02)
    return None, None


class TestDiagEnvelopeAdditions:
    """Top-level envelope additions: emit_ts + per-client request metadata."""

    def test_emit_ts_present_on_payload(self, diag_env):
        server_mod, _tmp, _db = diag_env
        from starlette.testclient import TestClient
        _short_window(server_mod)
        with TestClient(server_mod.app) as client:
            resp = client.get("/api/diag/sessions")
        body = resp.json()
        assert "emit_ts" in body
        assert "emit_ts_ms" in body
        assert isinstance(body["emit_ts"], (int, float))
        assert body["emit_ts"] > 0
        assert body["emit_ts_ms"] == int(body["emit_ts"] * 1000)

    def test_emit_ts_in_diag_request_event(self, diag_env):
        """The `diag:request` SSE payload must carry emit_ts so clients can
        compute clock_skew_ms."""
        server_mod, _tmp, _db = diag_env
        from starlette.testclient import TestClient
        _short_window(server_mod)
        bus = server_mod.event_bus

        with TestClient(server_mod.app) as client:
            client.get("/api/diag/sessions")
        # The most recent diag:request broadcast must include emit_ts.
        recent = bus.recent_broadcasts(topic="diag:request", limit=1)
        assert recent, "no diag:request broadcasts recorded"
        # We don't ship the payload in the recent log, but bus._last has the
        # serialised body.
        last = bus._last.get("diag:request")
        assert last is not None
        data = json.loads(last)
        assert "emit_ts" in data and "emit_ts_ms" in data

    def test_per_client_envelope_remote_addr_user_agent(self, diag_env):
        server_mod, tmp_path, db_path = diag_env
        from starlette.testclient import TestClient

        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir()
        jsonl = sess_dir / "auto-1.jsonl"
        _write_entries(jsonl, _toolish_entries())
        _insert_session(db_path, "auto-1", str(jsonl), file_offset=jsonl.stat().st_size)

        _short_window(server_mod)
        server_mod._DIAG_COLLECTION_WINDOW_SECONDS = 1.0

        with TestClient(server_mod.app) as client:
            def _post():
                deadline = time.time() + 2.0
                while time.time() < deadline:
                    if server_mod._DIAG_AGGREGATORS:
                        req_id = next(iter(server_mod._DIAG_AGGREGATORS))
                        client.post(
                            "/api/diag/client",
                            json={
                                "req_id": req_id,
                                "request_type": "session_markers",
                                "client_id": "tab-AA",
                                "payload": {
                                    "client_state": {"event_source_ready_state": 1},
                                    "sessions": {"auto-1": {"store_seq": 1, "tile_count": 0}},
                                },
                            },
                            headers={
                                "User-Agent": "TestUA/1.0 (diag)",
                                "Accept-Language": "en-US,en;q=0.9",
                            },
                        )
                        return
                    time.sleep(0.02)

            t = threading.Thread(target=_post)
            t.start()
            resp = client.get("/api/diag/sessions")
            t.join(timeout=3.0)
        body = resp.json()
        row = next(r for r in body["rows"] if r["session_id"] == "auto-1")
        assert len(row["clients"]) == 1
        c = row["clients"][0]
        assert c["client_id"] == "tab-AA"
        assert c["user_agent"] == "TestUA/1.0 (diag)"
        assert c["accept_language"] == "en-US,en;q=0.9"
        # Starlette TestClient reports remote_addr as None or a loopback addr.
        assert "remote_addr" in c
        assert "connection_id" in c
        assert "subscription_age_s" in c


class TestDiagBusBlockExtensions:
    """auto-wldnv bus additions: subscribers/recent_broadcasts/dedup/restore."""

    def test_subscribers_metadata_lists_active_subscribers(self, diag_env):
        server_mod, _tmp, _db = diag_env
        bus = server_mod.event_bus
        q1 = bus.subscribe(client_id="cid-1")
        q2 = bus.subscribe(client_id="cid-2")
        try:
            meta = bus.subscribers_metadata()
            assert len(meta) == 2
            cids = {m["client_id"] for m in meta}
            assert cids == {"cid-1", "cid-2"}
            for m in meta:
                assert m["age_s"] >= 0.0
                assert m["queue_depth"] >= 0
                assert m["dropped_count"] == 0
                assert m["connection_id"]
        finally:
            bus.unsubscribe(q1)
            bus.unsubscribe(q2)
        assert bus.subscribers_metadata() == []

    def test_recent_broadcasts_capped_at_ten_per_topic(self, diag_env):
        server_mod, _tmp, _db = diag_env
        bus = server_mod.event_bus

        async def _populate():
            for i in range(11):
                await bus.broadcast("test:rb", {"i": i}, dedup=False)
        asyncio.run(_populate())

        recent = bus.recent_broadcasts(topic="test:rb")
        assert len(recent) == 10
        # Monotonic: seqs ascend.
        seqs = [e["seq"] for e in recent]
        assert seqs == sorted(seqs)
        # Only the 10 most recent — the 11 broadcasts had successive seqs,
        # so the smallest one (the first) must have been evicted.
        assert seqs[0] > recent[0]["seq"] - 1 or len(seqs) == 10

    def test_dedup_skipped_total_increments(self, diag_env):
        server_mod, _tmp, _db = diag_env
        bus = server_mod.event_bus
        baseline = bus.dedup_skipped_total()

        async def _populate():
            await bus.broadcast("test:dedup", {"x": 1}, dedup=True)
            # Same payload, dedup=True → skipped.
            await bus.broadcast("test:dedup", {"x": 1}, dedup=True)
        asyncio.run(_populate())
        assert bus.dedup_skipped_total() == baseline + 1

    def test_restore_history_records_calls(self, diag_env):
        server_mod, tmp_path, _db = diag_env
        bus = server_mod.event_bus
        before = len(bus.restore_history())

        # Failure path: non-existent file.
        ok = bus.restore(tmp_path / "missing.state")
        assert ok is False
        # Success path: write a snapshot then restore.
        snap = tmp_path / "snap.state"

        async def _broadcast_one():
            await bus.broadcast("test:restore", {"v": 1}, dedup=False)
        asyncio.run(_broadcast_one())
        bus.snapshot(snap)
        ok2 = bus.restore(snap)
        assert ok2 is True

        history = bus.restore_history()
        assert len(history) == before + 2
        assert history[-2]["success"] is False
        assert history[-1]["success"] is True
        assert "ts" in history[-1]
        assert "seq_after" in history[-1]
        assert "epoch_after" in history[-1]

    def test_bus_block_includes_new_fields(self, diag_env):
        server_mod, _tmp, _db = diag_env
        from starlette.testclient import TestClient
        _short_window(server_mod)
        with TestClient(server_mod.app) as client:
            resp = client.get("/api/diag/sessions")
        body = resp.json()
        bus = body["bus"]
        for key in ("subscribers", "recent_broadcasts", "dedup_skipped_total",
                    "restore_history"):
            assert key in bus, f"bus missing {key}"
        assert isinstance(bus["subscribers"], list)
        assert isinstance(bus["recent_broadcasts"], list)
        assert isinstance(bus["dedup_skipped_total"], int)
        assert isinstance(bus["restore_history"], list)


class TestDiagFileBlockExtensions:
    """auto-wldnv file additions: tail_10, permissions, owner_uid, link_count, lines_in_*."""

    def test_file_block_has_new_fields(self, diag_env):
        server_mod, tmp_path, db_path = diag_env
        from starlette.testclient import TestClient

        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir()
        jsonl = sess_dir / "auto-1.jsonl"
        # 12 entries → tail_10 should keep 10, file lines == 12.
        entries = []
        for i in range(12):
            entries.append({"type": "assistant_text", "content": f"hi {i}",
                            "timestamp": f"2026-04-28T04:31:{20 + i:02d}.000Z"})
        _write_entries(jsonl, entries)
        _insert_session(db_path, "auto-1", str(jsonl), file_offset=jsonl.stat().st_size)

        _short_window(server_mod)
        with TestClient(server_mod.app) as client:
            resp = client.get("/api/diag/sessions?session=auto-1")
        body = resp.json()
        row = next(r for r in body["rows"] if r["session_id"] == "auto-1")
        f = row["file"]
        assert f["lines"] == 12
        assert len(f["tail_10"]) == 10
        assert len(f["tail_3"]) == 3  # backwards-compat
        assert f["permissions"] is not None
        assert f["permissions"].startswith("0o")
        assert isinstance(f["owner_uid"], int)
        assert isinstance(f["link_count"], int) and f["link_count"] >= 1
        assert f["lines_in_first_kb"] is not None
        assert f["lines_in_last_kb"] is not None


class TestDiagServerBlockExtensions:
    """auto-wldnv server additions: harness, parse_errors, lines_processed,
    inotify, enqueue_dedup, full_rescan, tail_10."""

    def test_server_block_has_new_fields(self, diag_env):
        server_mod, tmp_path, db_path = diag_env
        from starlette.testclient import TestClient

        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir()
        jsonl = sess_dir / "auto-1.jsonl"
        _write_entries(jsonl, _toolish_entries())
        _insert_session(db_path, "auto-1", str(jsonl), file_offset=jsonl.stat().st_size)

        _short_window(server_mod)
        with TestClient(server_mod.app) as client:
            resp = client.get("/api/diag/sessions?session=auto-1")
        body = resp.json()
        row = next(r for r in body["rows"] if r["session_id"] == "auto-1")
        s = row["server"]
        for key in (
            "harness", "harness_mismatch", "parse_errors_count",
            "last_parse_error", "lines_processed_total",
            "inotify_events_received", "last_inotify_event_ts",
            "enqueue_dedup_count", "last_enqueue_dedup_ts",
            "full_rescan_count", "last_full_rescan_ts", "tail_10",
        ):
            assert key in s, f"server missing {key}"

    def test_parse_errors_count_increments_on_corrupt_jsonl(self, diag_env):
        """Writing a malformed line bumps parse_errors_count via _tail_one."""
        server_mod, tmp_path, db_path = diag_env
        from tools.dashboard import session_monitor as monitor_mod

        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir()
        jsonl = sess_dir / "auto-1.jsonl"
        # Two valid + one corrupt + one valid.
        with open(jsonl, "w") as fh:
            fh.write(json.dumps({"type": "user", "content": "hi",
                                 "timestamp": "2026-04-28T00:00:00Z"}) + "\n")
            fh.write("this is not json — trip the parser\n")
            fh.write(json.dumps({"type": "assistant_text", "content": "ok",
                                 "timestamp": "2026-04-28T00:00:01Z"}) + "\n")
        _insert_session(db_path, "auto-1", str(jsonl), file_offset=0)

        ts = monitor_mod._TailState()
        monitor_mod.session_monitor._tail_states["auto-1"] = ts
        # Drive _tail_one directly so we don't need the inotify loop.
        row = monitor_mod.get_session("auto-1")
        ok, _ = monitor_mod.session_monitor._tail_one(row, ts)
        assert ts.parse_errors_count >= 1
        assert ts.last_parse_error is not None
        assert "json" in (ts.last_parse_error or "").lower() or \
               "expecting" in (ts.last_parse_error or "").lower()
        # lines_processed_total counts every non-empty line read.
        assert ts.lines_processed_total == 3

    def test_harness_field_matches_db(self, diag_env):
        server_mod, tmp_path, db_path = diag_env
        from starlette.testclient import TestClient
        from tools.dashboard.dao import dashboard_db as db_mod

        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir()
        jsonl = sess_dir / "auto-1.jsonl"
        _write_entries(jsonl, _toolish_entries())
        _insert_session(db_path, "auto-1", str(jsonl), file_offset=jsonl.stat().st_size)
        # Set an explicit harness column value.
        conn = db_mod.get_conn()
        conn.execute("UPDATE tmux_sessions SET harness=? WHERE tmux_name=?",
                     ("claude", "auto-1"))
        conn.commit()

        _short_window(server_mod)
        with TestClient(server_mod.app) as client:
            resp = client.get("/api/diag/sessions?session=auto-1")
        body = resp.json()
        row = next(r for r in body["rows"] if r["session_id"] == "auto-1")
        assert row["server"]["harness"] == "claude"

    def test_harness_mismatch_flag_for_rollout_jsonl_as_claude(self, diag_env):
        """A rollout-* JSONL registered as harness=claude is the canonical
        mismatch pattern from the auto-f8r9l rollout. The diag block must
        flag it."""
        server_mod, tmp_path, db_path = diag_env
        from starlette.testclient import TestClient
        from tools.dashboard.dao import dashboard_db as db_mod

        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir()
        jsonl = sess_dir / "rollout-2026-04-28T04-30-00-abcd.jsonl"
        _write_entries(jsonl, _toolish_entries())
        _insert_session(db_path, "auto-1", str(jsonl), file_offset=jsonl.stat().st_size)
        conn = db_mod.get_conn()
        conn.execute("UPDATE tmux_sessions SET harness=? WHERE tmux_name=?",
                     ("claude", "auto-1"))
        conn.commit()

        _short_window(server_mod)
        with TestClient(server_mod.app) as client:
            resp = client.get("/api/diag/sessions?session=auto-1")
        body = resp.json()
        row = next(r for r in body["rows"] if r["session_id"] == "auto-1")
        assert row["server"]["harness"] == "claude"
        assert row["server"]["harness_mismatch"] is True

    def test_recent_processed_maxlen_is_ten(self, diag_env):
        """Bumped from 3 → 10 in auto-wldnv so server tail_10 has room."""
        from tools.dashboard import session_monitor as monitor_mod
        ts = monitor_mod._TailState()
        # The deque should now accept up to 10 entries.
        assert ts.recent_processed.maxlen == 10
        for i in range(15):
            ts.recent_processed.append(("t", str(i), f"id-{i}"))
        assert len(ts.recent_processed) == 10
        last = list(ts.recent_processed)[-1]
        assert last[2] == "id-14"

    def test_enqueue_dedup_count_increments(self, diag_env):
        """When a queued message is followed by a matching user entry, the
        dedup path drops the user entry and bumps enqueue_dedup_count."""
        server_mod, tmp_path, db_path = diag_env
        from tools.dashboard import session_monitor as monitor_mod

        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir()
        jsonl = sess_dir / "auto-1.jsonl"
        jsonl.touch()
        _insert_session(db_path, "auto-1", str(jsonl), file_offset=0)

        ts = monitor_mod._TailState()
        monitor_mod.session_monitor._tail_states["auto-1"] = ts
        # Run the dedup logic directly via _process_tail_entries.
        row = monitor_mod.get_session("auto-1")
        new_entries = [
            {"type": "user", "content": "hello", "queued": True,
             "timestamp": "2026-04-28T00:00:00Z"},
            {"type": "user", "content": "hello",
             "timestamp": "2026-04-28T00:00:01Z"},
        ]
        # Patch out the broadcast so we don't need a running event bus loop.
        monitor_mod.session_monitor._event_bus = None
        asyncio.run(monitor_mod.session_monitor._process_tail_entries(
            "auto-1", row, ts, new_entries,
        ))
        assert ts.enqueue_dedup_count == 1
        assert ts.last_enqueue_dedup_ts > 0


class TestDiagTopicBlock:
    def test_topic_recent_broadcasts_for_session_present(self, diag_env):
        server_mod, tmp_path, db_path = diag_env
        from starlette.testclient import TestClient

        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir()
        jsonl = sess_dir / "auto-1.jsonl"
        _write_entries(jsonl, _toolish_entries())
        _insert_session(db_path, "auto-1", str(jsonl), file_offset=jsonl.stat().st_size)

        bus = server_mod.event_bus

        async def _seed():
            for i in range(3):
                await bus.broadcast(
                    "session:messages", {"session_id": "auto-1", "i": i},
                    dedup=False,
                )
        asyncio.run(_seed())

        _short_window(server_mod)
        from starlette.testclient import TestClient
        with TestClient(server_mod.app) as client:
            resp = client.get("/api/diag/sessions?session=auto-1")
        body = resp.json()
        row = next(r for r in body["rows"] if r["session_id"] == "auto-1")
        assert "recent_broadcasts_for_session" in row["topic"]
        recent = row["topic"]["recent_broadcasts_for_session"]
        assert isinstance(recent, list)
        assert any(e["topic"] == "session:messages" for e in recent)


class TestDiagTextFormatGlyphs:
    def test_text_table_has_compact_glyphs(self, diag_env):
        server_mod, tmp_path, db_path = diag_env
        from starlette.testclient import TestClient

        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir()
        jsonl = sess_dir / "auto-1.jsonl"
        _write_entries(jsonl, _toolish_entries())
        _insert_session(db_path, "auto-1", str(jsonl), file_offset=jsonl.stat().st_size)

        _short_window(server_mod)
        with TestClient(server_mod.app) as client:
            resp = client.get("/api/diag/sessions?format=text")
        text = resp.text
        # Compact glyphs from the spec.
        assert "parse_err=" in text
        assert "replay=" in text
        assert "dedup=" in text
        assert "harness=" in text
        # Bus-line glyphs.
        assert "dedup_skip=" in text
        # Width: no line wider than 200 cols.
        for line in text.splitlines():
            assert len(line) <= 200, f"line too wide ({len(line)}): {line!r}"
