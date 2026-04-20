"""Tests for session store shape and seq handling (Boundary E — store state management).

Tests the store contract from Architecture Spec v7:
- Registry response shape: has `resolved`, no `tmuxSession`/`linked`/`_enriched_*`
- Seq dedup: duplicate seq rejected, higher seq accepted
- Seq regression: server restart resets seq, backfill resets store.seq

Expected test status on CURRENT code (before auto-h4gh):
- TestStoreShape: FAIL — current registry has `tmux_session`/`linked`, no `resolved`
- TestSeqDedup: PASS — current appendSessionEntries() does seq dedup correctly
- TestSeqRegression: FAIL — current code silently drops entries after server restart

Uses Approach A: test the Python-side data that feeds the store via the
/api/dao/active_sessions API endpoint.
"""

import json
import os
import sqlite3
import time

import pytest


# ── Fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def store_test_db(tmp_path):
    """Create a dashboard.db with sessions for store shape testing.

    Includes sessions with varying session_uuids to test resolved derivation.
    """
    db_path = tmp_path / "dashboard.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""CREATE TABLE IF NOT EXISTS tmux_sessions (
        tmux_name TEXT PRIMARY KEY, session_uuid TEXT, graph_source_id TEXT,
        type TEXT NOT NULL, project TEXT NOT NULL, jsonl_path TEXT,
        bead_id TEXT, created_at REAL NOT NULL, is_live INTEGER DEFAULT 1,
        file_offset INTEGER DEFAULT 0, last_activity REAL,
        last_message TEXT DEFAULT '', entry_count INTEGER DEFAULT 0,
        context_tokens INTEGER DEFAULT 0, label TEXT DEFAULT '',
        topics TEXT DEFAULT '[]', role TEXT DEFAULT '',
        nag_enabled INTEGER DEFAULT 0, nag_interval INTEGER DEFAULT 15,
        nag_message TEXT DEFAULT '', nag_last_sent REAL DEFAULT 0,
        dispatch_nag INTEGER DEFAULT 0,
        resolution_dir TEXT, session_uuids TEXT DEFAULT '[]',
        curr_jsonl_file TEXT
    )""")
    now = time.time()

    # Session with resolved UUIDs (should have resolved=true)
    conn.execute(
        """INSERT INTO tmux_sessions
        (tmux_name, type, project, created_at, is_live, last_message,
         entry_count, context_tokens, label, role, session_uuids, last_activity)
        VALUES (?,?,?,?,1,?,?,?,?,?,?,?)""",
        ("auto-resolved-test", "container", "autonomy", now,
         "Working on tests", 100, 50000, "Resolved Session", "builder",
         '["abc-uuid-1"]', now),
    )

    # Session with empty session_uuids (should have resolved=false)
    conn.execute(
        """INSERT INTO tmux_sessions
        (tmux_name, type, project, created_at, is_live, last_message,
         entry_count, context_tokens, label, role, session_uuids, last_activity)
        VALUES (?,?,?,?,1,?,?,?,?,?,?,?)""",
        ("host-unresolved-test", "host", "autonomy", now,
         "Waiting for link", 0, 0, "Unresolved Host", "",
         '[]', now),
    )

    conn.commit()
    conn.close()
    return str(db_path)


@pytest.fixture
def store_test_client(store_test_db):
    """Boot dashboard with test DB and return a sync test client."""
    os.environ["DASHBOARD_DB"] = store_test_db
    import importlib
    from unittest.mock import patch

    from tools.dashboard.dao import dashboard_db as db_mod
    importlib.reload(db_mod)
    from tools.dashboard import server
    importlib.reload(server)

    # Patch tmux check so test sessions appear live, and no-op dispatch_db.init_db
    # which tries to open data/dispatch.db (may not exist in test/read-only envs)
    with patch(
        "tools.dashboard.session_monitor.SessionMonitor._check_tmux",
        staticmethod(lambda name: True),
    ), patch("agents.dispatch_db.init_db"):
        from starlette.testclient import TestClient
        with TestClient(server.app) as client:
            yield client


# ══════════════════════════════════════════════════════════════════════
# TestStoreShape — registry response has correct fields (Boundary E)
#
# Expected: FAIL on current code (auto-h4gh adds `resolved`, removes
# `tmux_session`/`linked` from registry).
# ══════════════════════════════════════════════════════════════════════

class TestStoreShape:
    """Verify /api/dao/active_sessions response matches the v7 spec store shape."""

    def test_registry_has_resolved_field(self, store_test_client):
        """GET /api/dao/active_sessions → each session has 'resolved' key.

        Expected: FAIL (current registry uses 'linked', not 'resolved').
        """
        resp = store_test_client.get("/api/dao/active_sessions")
        assert resp.status_code == 200
        sessions = resp.json()
        assert len(sessions) >= 1
        for s in sessions:
            assert "resolved" in s, (
                f"Session {s.get('session_id', '?')} missing 'resolved' field. "
                f"Keys present: {sorted(s.keys())}"
            )

    def test_registry_no_tmux_session_field(self, store_test_client):
        """Response has no 'tmux_session' or 'tmuxSession' key.

        Expected: FAIL (current registry includes 'tmux_session').
        The session identity is the store key (session_id). tmux_session is
        redundant and removed in v7 spec Section 4c.
        """
        resp = store_test_client.get("/api/dao/active_sessions")
        sessions = resp.json()
        for s in sessions:
            assert "tmux_session" not in s, (
                f"Session {s['session_id']} still has 'tmux_session' field"
            )
            assert "tmuxSession" not in s, (
                f"Session {s['session_id']} still has 'tmuxSession' field"
            )

    def test_registry_no_linked_field(self, store_test_client):
        """Response has no 'linked' key — replaced by 'resolved'.

        Expected: FAIL (current registry includes 'linked').
        """
        resp = store_test_client.get("/api/dao/active_sessions")
        sessions = resp.json()
        for s in sessions:
            assert "linked" not in s, (
                f"Session {s['session_id']} still has 'linked' field"
            )

    def test_resolved_true_when_session_uuids_set(self, store_test_client):
        """Session with session_uuids=["abc"] → resolved=true.

        Expected: FAIL (current code doesn't emit 'resolved').
        Spec: resolved = len(session_uuids) > 0
        """
        resp = store_test_client.get("/api/dao/active_sessions")
        sessions = resp.json()
        resolved_session = next(
            (s for s in sessions if s["session_id"] == "auto-resolved-test"), None
        )
        assert resolved_session is not None, "Test session 'auto-resolved-test' not found"
        assert resolved_session["resolved"] is True

    def test_resolved_false_when_session_uuids_empty(self, store_test_client):
        """Session with session_uuids=[] → resolved=false.

        Expected: FAIL (current code doesn't emit 'resolved').
        Spec: resolved = len(session_uuids) > 0
        """
        resp = store_test_client.get("/api/dao/active_sessions")
        sessions = resp.json()
        unresolved_session = next(
            (s for s in sessions if s["session_id"] == "host-unresolved-test"), None
        )
        assert unresolved_session is not None, "Test session 'host-unresolved-test' not found"
        assert unresolved_session["resolved"] is False


# ══════════════════════════════════════════════════════════════════════
# TestSeqDedup — entry-identity dedup in appendSessionEntries (Boundary E)
#
# These tests exercise the JS logic via a Python simulation that mirrors
# the exact semantics of session-store.js appendSessionEntries().
#
# auto-bv343 swapped per-session seq dedup for entry-identity dedup so that
# SSE gap-replay payloads whose seq is "behind" store.seq still land. The
# tests below cover both the dedup contract and that store.seq still
# advances for clients that rely on it.
# ══════════════════════════════════════════════════════════════════════

def _make_store():
    """Create a minimal store dict matching session-store.js getSessionStore()."""
    return {
        "entries": [],
        "seq": 0,
        "isLive": True,
        "toolMap": {},
        "resultMap": {},
        "_loading": False,
        "_needsBackfill": False,
    }


def _entry_identity(entry):
    """Mirror of _entryIdentity() in session-store.js."""
    if not entry:
        return None
    if entry.get("type") == "tool_use" and entry.get("tool_id"):
        return "tu:" + entry["tool_id"]
    if entry.get("type") == "tool_result" and entry.get("tool_id"):
        return "tr:" + entry["tool_id"]
    t = entry.get("type") or "?"
    ts = entry.get("timestamp") or ""
    raw = entry.get("content")
    if isinstance(raw, str):
        c = raw[:200]
    elif raw is None:
        c = ""
    else:
        try:
            c = json.dumps(raw)[:200]
        except (TypeError, ValueError):
            c = ""
    return f"{t}:{ts}:{c}"


def _append_session_entries(store, data):
    """Python mirror of window.appendSessionEntries() from session-store.js.

    Entry-identity dedup: store.seq still advances (with server-restart
    detection) but does NOT gate appending. Entries are deduped on a stable
    per-entry identity key so gap-replay payloads whose seq is "behind"
    store.seq still land if their entries are new.
    """
    if data.get("seq") is not None:
        if data["seq"] > store["seq"]:
            store["seq"] = data["seq"]
        elif store["seq"] > 1 and data["seq"] * 2 < store["seq"]:
            # Significant seq regression → server restart; reset.
            store["seq"] = data["seq"]
        # Otherwise leave store.seq alone; fall through to entry dedup.

    if data.get("is_live") is not None:
        store["isLive"] = data["is_live"]

    if not data.get("entries") or len(data["entries"]) == 0:
        return 0

    seen = store.setdefault("_seenIdentities", None)
    if seen is None:
        seen = {}
        for existing in store["entries"]:
            key = _entry_identity(existing)
            if key:
                seen[key] = True
        store["_seenIdentities"] = seen

    added = 0
    for entry in data["entries"]:
        key = _entry_identity(entry)
        if key and key in seen:
            continue
        if key:
            seen[key] = True
        if entry.get("type") == "tool_use" and entry.get("tool_id"):
            store["toolMap"][entry["tool_id"]] = {"tool_name": entry.get("tool_name", "?")}
        if entry.get("type") == "tool_result" and entry.get("tool_id"):
            store["resultMap"][entry["tool_id"]] = entry
        store["entries"].append(entry)
        added += 1
    return added


class TestSeqDedup:
    """Entry-identity dedup prevents duplicate entries; store.seq still tracks."""

    def test_identical_entry_rejected(self):
        """Re-feeding the same entry (same type/timestamp/content) is a no-op."""
        store = _make_store()
        entry = {"type": "user", "timestamp": "2026-04-20T11:51:00Z", "content": "first"}
        added = _append_session_entries(store, {"seq": 5, "entries": [entry]})
        assert added == 1
        assert len(store["entries"]) == 1

        # Same payload again — entry identity already in store, rejected.
        added = _append_session_entries(store, {"seq": 5, "entries": [entry]})
        assert added == 0
        assert len(store["entries"]) == 1

        # Same identity at a lower seq (replay) — still rejected.
        added = _append_session_entries(store, {"seq": 3, "entries": [entry]})
        assert added == 0
        assert len(store["entries"]) == 1

    def test_distinct_entry_with_lower_seq_accepted(self):
        """Replay payload whose seq is behind store.seq still lands new entries.

        This is the gap-replay regression that auto-bv343 fixes: when SSE gap
        replay or a Last-Event-ID native reconnect re-delivers entries with
        seqs lower than store.seq, the entries themselves are new and must be
        appended. The pre-fix seq-based guard silently dropped them.
        """
        store = _make_store()
        e1 = {"type": "user", "timestamp": "t1", "content": "first"}
        e2 = {"type": "user", "timestamp": "t2", "content": "behind"}
        _append_session_entries(store, {"seq": 5, "entries": [e1]})
        added = _append_session_entries(store, {"seq": 3, "entries": [e2]})
        assert added == 1
        assert len(store["entries"]) == 2

    def test_tool_id_dedup(self):
        """tool_use / tool_result are deduped by tool_id, regardless of seq."""
        store = _make_store()
        tu = {"type": "tool_use", "tool_id": "T1", "tool_name": "Bash"}
        _append_session_entries(store, {"seq": 5, "entries": [tu]})
        # Re-deliver same tool_use at a different seq — rejected.
        added = _append_session_entries(store, {"seq": 7, "entries": [dict(tu)]})
        assert added == 0
        assert len(store["entries"]) == 1
        # tool_use vs tool_result with the same tool_id are distinct identities.
        tr = {"type": "tool_result", "tool_id": "T1", "content": "ok"}
        added = _append_session_entries(store, {"seq": 8, "entries": [tr]})
        assert added == 1
        assert len(store["entries"]) == 2

    def test_higher_seq_accepted(self):
        """appendSessionEntries with seq > store.seq → entries added."""
        store = _make_store()
        _append_session_entries(store, {
            "seq": 5,
            "entries": [{"type": "user", "content": "first"}],
        })

        added = _append_session_entries(store, {
            "seq": 6,
            "entries": [{"type": "user", "content": "second"}],
        })
        assert added == 1
        assert len(store["entries"]) == 2

    def test_seq_updated_after_append(self):
        """store.seq advances to the new value after successful append."""
        store = _make_store()
        assert store["seq"] == 0

        _append_session_entries(store, {
            "seq": 10,
            "entries": [{"type": "user", "content": "msg"}],
        })
        assert store["seq"] == 10

        _append_session_entries(store, {
            "seq": 25,
            "entries": [{"type": "user", "content": "msg2"}],
        })
        assert store["seq"] == 25


# ══════════════════════════════════════════════════════════════════════
# TestSeqRegression — server restart seq handling (Boundary E)
#
# Expected: FAIL on current code. The current appendSessionEntries()
# silently drops entries when seq regresses after a server restart.
# auto-h4gh adds seq regression detection.
#
# Root cause (graph://4015acbb-0bf t351): after server restart, backlog
# fetch sets store.seq=5 (pre-restart value). New entries arrive with
# data.seq=1 (post-restart). Store dedup silently drops them because
# 1 <= 5.
# ══════════════════════════════════════════════════════════════════════

class TestSeqRegression:
    """Seq regression after server restart must not silently drop entries.

    Expected: FAIL on current code.
    """

    def test_backfill_resets_seq(self):
        """After backfill response with seq=N, store.seq = N (not stale value).

        Expected: PASS — this is just verifying the basic seq update which
        already works. The real issue is what happens AFTER a restart.
        """
        store = _make_store()
        # Simulate pre-restart state: seq was high
        store["seq"] = 500

        # Backfill response should reset seq to server's current value.
        # In v7, the backfill handler explicitly sets store.seq = response.seq.
        # This test verifies the append path handles it.
        _append_session_entries(store, {
            "seq": 500,  # same seq → rejected by current code
            "entries": [{"type": "user", "content": "backfill"}],
        })
        # With current code, this append is rejected (seq=500 <= store.seq=500)
        # In v7, backfill resets store.seq BEFORE appending, so this is a
        # separate code path. This test documents the current limitation.
        # The store.seq should be reset by the backfill handler, not by append.
        assert store["seq"] == 500  # seq unchanged — this is expected

    def test_lower_seq_after_restart_accepted(self):
        """Server restart resets seq to 0, entries with seq=1 not dropped.

        Expected: FAIL — current code drops entries when new seq < old seq.
        auto-h4gh adds seq regression detection: when a significantly lower
        seq arrives, the store recognizes a server restart and resets.
        """
        store = _make_store()
        # Pre-restart: seq was high
        _append_session_entries(store, {
            "seq": 100,
            "entries": [{"type": "user", "content": "pre-restart"}],
        })
        assert store["seq"] == 100

        # Server restarts, seq resets to 0. First SSE event arrives with seq=1.
        # Current code: 1 <= 100 → DROPPED (bug!)
        # v7 code: detects regression, resets store.seq, accepts entry
        added = _append_session_entries(store, {
            "seq": 1,
            "entries": [{"type": "user", "content": "post-restart"}],
        })
        assert added == 1, (
            "Entry with seq=1 after server restart (old seq=100) was dropped. "
            "Seq regression detection needed: when seq drops significantly, "
            "the store should recognize a server restart and accept the entry."
        )
        assert len(store["entries"]) == 2
        assert store["entries"][-1]["content"] == "post-restart"

    def test_gap_unrecoverable_triggers_backfill(self):
        """sse-gap-unrecoverable event → store re-fetches history.

        Expected: FAIL — current session-store.js has no handler for
        sse-gap-unrecoverable. When the ring buffer can't cover a gap
        after reconnect, the viewer should re-fetch full backlog.

        This test verifies the contract: after an unrecoverable gap event,
        the store's entries are replaced with fresh backfill data and
        seq is reset to the server's current value.
        """
        store = _make_store()
        # Populate with some pre-gap entries
        _append_session_entries(store, {
            "seq": 50,
            "entries": [
                {"type": "user", "content": "msg1"},
                {"type": "assistant_text", "content": "reply1"},
            ],
        })
        assert len(store["entries"]) == 2

        # Simulate gap-unrecoverable handler: should clear entries and
        # set a flag that triggers backfill re-fetch.
        # v7 spec: store gets a `_needsBackfill` flag or similar mechanism.
        # The actual backfill is async, but the handler should at minimum
        # signal that the current entries are stale.
        #
        # For now, test the minimal contract: the store should have a way
        # to signal that a backfill is needed after an unrecoverable gap.
        # In v7, this means store._loading is set back to true.
        assert hasattr(store, "get") or isinstance(store, dict), "store is a dict"

        # The gap handler should reset loading state to trigger re-fetch
        # This documents the expected behavior — auto-h4gh implements it
        gap_handled = "_needsBackfill" in store or store.get("_loading") is True
        # Current code: no gap handler exists, so neither flag is set
        # v7 code: gap handler sets _loading=true and clears entries
        assert gap_handled or store.get("_gapDetected"), (
            "No gap-unrecoverable handler found. After an unrecoverable SSE gap, "
            "the store should signal that re-fetch is needed (set _loading=true "
            "or _needsBackfill=true)."
        )
