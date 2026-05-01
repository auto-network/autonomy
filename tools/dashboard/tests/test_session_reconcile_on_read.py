"""Read-side reconcile + CrossTalk envelope correctness (auto-4nr14).

Companion to ``test_graph_source_id_reconcile.py`` (auto-4jpa8). The
writer-side reconciler keeps ``tmux_sessions.graph_source_id`` honest on
the 5-min tick, but several read paths still hand the column to a caller
*between* ticks. If the column has drifted (a UUID no org DB has) or
raced (empty / NULL pending ingest), every consumer of those surfaces
sees a 404 when it tries to follow the link.

This module covers the read-side defenses:

  * :func:`reconcile_session_graph_source_id` — just-in-time read-side
    repair; resolves the stored ID against the org DBs, otherwise looks
    it up via ``sources.file_path = jsonl_path`` and persists the fix.
  * :func:`get_source_max_turn_number` — ``MAX(turn_number)`` over
    thoughts ∪ derivations for a resolved source ID. The CrossTalk
    envelope's ``turn=`` attribute uses this counter (graph turns), NOT
    ``tmux_sessions.entry_count`` (JSONL viewer-tail line count, which
    is ~10–20× larger because graph ingest filters tool-use messages).
  * ``/api/session/{tmux_name}`` — the session detail endpoint MUST
    surface the reconciled ID, not the drifted stored value.
  * ``/api/crosstalk/send`` — CrossTalk envelopes MUST carry the
    reconciled source ID and the graph ``turn_number``, not the
    drifted ID and ``entry_count``.

Live evidence at the time of filing: ``auto-0428-005821`` carried
``graph_source_id = d15ca523-643e-…`` in dashboard.db while the real
source for the JSONL was ``a5134fd1-7c42-…``; CrossTalk envelopes from
that session shipped ``source="d15ca523-…"`` (drifted) and
``turn="12342"`` (entry_count) for a graph that had only 679 turns.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

import pytest


# ══════════════════════════════════════════════════════════════════════
# Fixture scaffolding
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
def _evict_pooled_orgs():
    """Per-org GraphDB connections are pooled for the process lifetime."""
    from tools.graph.db import GraphDB
    GraphDB.close_all_pooled()
    yield
    GraphDB.close_all_pooled()


def _init_dashboard_db(db_path: Path) -> None:
    """Minimal ``tmux_sessions`` schema. Mirrors ``dashboard_db._SCHEMA``."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("""CREATE TABLE IF NOT EXISTS tmux_sessions (
        tmux_name TEXT PRIMARY KEY,
        session_uuid TEXT,
        graph_source_id TEXT,
        type TEXT NOT NULL,
        project TEXT NOT NULL,
        jsonl_path TEXT,
        bead_id TEXT,
        created_at REAL NOT NULL,
        is_live INTEGER DEFAULT 1,
        file_offset INTEGER DEFAULT 0,
        last_activity REAL,
        last_message TEXT DEFAULT '',
        entry_count INTEGER DEFAULT 0,
        context_tokens INTEGER DEFAULT 0,
        label TEXT DEFAULT '',
        topics TEXT DEFAULT '[]',
        role TEXT DEFAULT '',
        nag_enabled INTEGER DEFAULT 0,
        nag_interval INTEGER DEFAULT 15,
        nag_message TEXT DEFAULT '',
        nag_last_sent REAL DEFAULT 0,
        dispatch_nag INTEGER DEFAULT 0,
        resolution_dir TEXT,
        session_uuids TEXT DEFAULT '[]',
        curr_jsonl_file TEXT,
        activity_state TEXT DEFAULT 'idle'
    )""")
    conn.commit()
    conn.close()


def _insert_row(
    db_path: Path,
    tmux_name: str,
    *,
    jsonl_path: str | None,
    graph_source_id: str | None,
    label: str = "",
    entry_count: int = 0,
) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO tmux_sessions"
        " (tmux_name, type, project, jsonl_path, graph_source_id,"
        "  created_at, is_live, label, entry_count)"
        " VALUES (?, 'host', 'autonomy', ?, ?, ?, 1, ?, ?)",
        (tmux_name, jsonl_path, graph_source_id, time.time(), label, entry_count),
    )
    conn.commit()
    conn.close()


def _insert_org_source(
    db_path: Path,
    *,
    source_id: str,
    file_path: str,
) -> None:
    """Insert a session source row into a per-org GraphDB."""
    from tools.graph.db import GraphDB
    g = GraphDB(db_path)
    g.conn.execute(
        "INSERT INTO sources"
        " (id, type, platform, project, title, file_path,"
        "  metadata, created_at, ingested_at, last_activity_at)"
        " VALUES (?, 'session', 'claude-code', 'autonomy', 'test', ?, ?,"
        "         '2026-04-30T00:00:00Z', '2026-04-30T00:00:00Z', '2026-04-30T00:00:00Z')",
        (source_id, file_path, json.dumps({"session_uuid": Path(file_path).stem})),
    )
    g.commit()
    g.close()


def _insert_thought(
    db_path: Path,
    *,
    source_id: str,
    turn_number: int,
    thought_id: str | None = None,
) -> None:
    """Insert a thought row at ``turn_number`` for ``source_id``."""
    from tools.graph.db import GraphDB
    g = GraphDB(db_path)
    g.conn.execute(
        "INSERT INTO thoughts (id, source_id, content, role, turn_number)"
        " VALUES (?, ?, 'test thought', 'user', ?)",
        (thought_id or f"th-{source_id}-{turn_number}", source_id, turn_number),
    )
    g.commit()
    g.close()


def _insert_derivation(
    db_path: Path,
    *,
    source_id: str,
    turn_number: int,
    deriv_id: str | None = None,
) -> None:
    """Insert a derivation row at ``turn_number`` for ``source_id``."""
    from tools.graph.db import GraphDB
    g = GraphDB(db_path)
    g.conn.execute(
        "INSERT INTO derivations (id, source_id, content, model, turn_number)"
        " VALUES (?, ?, 'test deriv', 'claude', ?)",
        (deriv_id or f"de-{source_id}-{turn_number}", source_id, turn_number),
    )
    g.commit()
    g.close()


@pytest.fixture
def setup_env(tmp_path, monkeypatch):
    """Build dashboard.db + an empty orgs/ root the helpers can see."""
    db_path = tmp_path / "dashboard.db"
    _init_dashboard_db(db_path)
    orgs_dir = tmp_path / "orgs"
    orgs_dir.mkdir()
    monkeypatch.setenv("DASHBOARD_DB", str(db_path))
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    from tools.dashboard.dao import dashboard_db as ddb
    importlib.reload(ddb)
    yield tmp_path, db_path, orgs_dir


# ══════════════════════════════════════════════════════════════════════
# reconcile_session_graph_source_id — read-side helper unit tests
# ══════════════════════════════════════════════════════════════════════


class TestReconcileSessionGraphSourceId:
    """Defense-in-depth read-side complement to the writer-side reconciler."""

    def test_returns_stored_id_when_already_resolves(self, setup_env):
        """The happy path: stored ID resolves, no lookup needed."""
        tmp_path, db_path, orgs_dir = setup_env
        jsonl = str(tmp_path / "ok.jsonl")
        Path(jsonl).touch()
        _insert_org_source(
            orgs_dir / "autonomy.db",
            source_id="happy-id",
            file_path=jsonl,
        )
        _insert_row(
            db_path, "auto-happy",
            jsonl_path=jsonl,
            graph_source_id="happy-id",
        )
        from tools.dashboard.dao import dashboard_db as ddb
        session = ddb.get_session("auto-happy")

        assert ddb.reconcile_session_graph_source_id(session) == "happy-id"

    def test_repairs_drifted_id_in_place(self, setup_env):
        """When the stored ID resolves nowhere but jsonl_path does, the
        helper returns the canonical ID AND persists the repair so the
        next caller (and the writer-side reconciler) see consistent
        state."""
        tmp_path, db_path, orgs_dir = setup_env
        jsonl = str(tmp_path / "drift.jsonl")
        Path(jsonl).touch()
        _insert_org_source(
            orgs_dir / "autonomy.db",
            source_id="real-id",
            file_path=jsonl,
        )
        _insert_row(
            db_path, "auto-drift",
            jsonl_path=jsonl,
            graph_source_id="bogus-uuid-no-org-has-this",
        )
        from tools.dashboard.dao import dashboard_db as ddb
        session = ddb.get_session("auto-drift")

        result = ddb.reconcile_session_graph_source_id(session)

        assert result == "real-id"
        # Persist on read so subsequent reads + the SSE broadcast see truth.
        repaired_row = ddb.get_session("auto-drift")
        assert repaired_row["graph_source_id"] == "real-id"

    def test_backfills_empty_id(self, setup_env):
        """Race case: registration completed before ingest. Empty ID gets
        filled on read."""
        tmp_path, db_path, orgs_dir = setup_env
        jsonl = str(tmp_path / "race.jsonl")
        Path(jsonl).touch()
        _insert_org_source(
            orgs_dir / "autonomy.db",
            source_id="race-id",
            file_path=jsonl,
        )
        _insert_row(
            db_path, "auto-race",
            jsonl_path=jsonl,
            graph_source_id="",
        )
        from tools.dashboard.dao import dashboard_db as ddb
        session = ddb.get_session("auto-race")

        assert ddb.reconcile_session_graph_source_id(session) == "race-id"
        assert ddb.get_session("auto-race")["graph_source_id"] == "race-id"

    def test_empty_when_jsonl_not_yet_ingested(self, setup_env):
        """If the JSONL has not been ingested yet, the helper returns ""
        rather than leak the unverifiable stored value. The next tick
        will repair the column writer-side."""
        tmp_path, db_path, _ = setup_env
        _insert_row(
            db_path, "auto-pending",
            jsonl_path=str(tmp_path / "not-ingested.jsonl"),
            graph_source_id="",
        )
        from tools.dashboard.dao import dashboard_db as ddb
        session = ddb.get_session("auto-pending")

        assert ddb.reconcile_session_graph_source_id(session) == ""

    def test_empty_when_drifted_and_no_jsonl_path(self, setup_env):
        """A drifted ID with no jsonl_path can't be repaired — return
        empty rather than leak the lie out the read surface."""
        tmp_path, db_path, _ = setup_env
        _insert_row(
            db_path, "auto-orphan",
            jsonl_path=None,
            graph_source_id="bogus-floating-id",
        )
        from tools.dashboard.dao import dashboard_db as ddb
        session = ddb.get_session("auto-orphan")

        assert ddb.reconcile_session_graph_source_id(session) == ""

    def test_handles_none_session(self, setup_env):
        from tools.dashboard.dao import dashboard_db as ddb
        assert ddb.reconcile_session_graph_source_id(None) == ""


# ══════════════════════════════════════════════════════════════════════
# get_source_max_turn_number — graph turn counter
# ══════════════════════════════════════════════════════════════════════


class TestGetSourceMaxTurnNumber:
    """Per auto-4nr14 §B, CrossTalk envelopes must use the graph
    turn_number, NOT entry_count. Distinct counters: graph ingest filters
    tool_use / tool_result messages, so turn_number is ~10-20× smaller
    than entry_count for typical sessions."""

    def test_max_across_thoughts_and_derivations(self, setup_env):
        """The maximum is taken over thoughts ∪ derivations."""
        tmp_path, _, orgs_dir = setup_env
        jsonl = str(tmp_path / "turns.jsonl")
        Path(jsonl).touch()
        org_db = orgs_dir / "autonomy.db"
        _insert_org_source(
            org_db,
            source_id="turn-id",
            file_path=jsonl,
        )
        # Interleaved thought/derivation turns; max is in derivations.
        _insert_thought(org_db, source_id="turn-id", turn_number=1)
        _insert_derivation(org_db, source_id="turn-id", turn_number=2)
        _insert_thought(org_db, source_id="turn-id", turn_number=3)
        _insert_derivation(org_db, source_id="turn-id", turn_number=4)

        from tools.dashboard.dao import dashboard_db as ddb

        assert ddb.get_source_max_turn_number("turn-id") == 4

    def test_max_when_only_thoughts(self, setup_env):
        tmp_path, _, orgs_dir = setup_env
        jsonl = str(tmp_path / "tonly.jsonl")
        Path(jsonl).touch()
        org_db = orgs_dir / "autonomy.db"
        _insert_org_source(org_db, source_id="t-only", file_path=jsonl)
        _insert_thought(org_db, source_id="t-only", turn_number=7)

        from tools.dashboard.dao import dashboard_db as ddb

        assert ddb.get_source_max_turn_number("t-only") == 7

    def test_none_when_source_unknown(self, setup_env):
        """A source ID no org DB recognises returns None — callers must
        omit the turn= attribute rather than fall back to entry_count."""
        from tools.dashboard.dao import dashboard_db as ddb

        assert ddb.get_source_max_turn_number("unknown-id") is None

    def test_none_when_source_has_no_turns(self, setup_env):
        """Source exists but no thoughts/derivations yet (newly ingested
        empty session)."""
        tmp_path, _, orgs_dir = setup_env
        jsonl = str(tmp_path / "empty.jsonl")
        Path(jsonl).touch()
        _insert_org_source(
            orgs_dir / "autonomy.db",
            source_id="empty-id",
            file_path=jsonl,
        )
        from tools.dashboard.dao import dashboard_db as ddb

        assert ddb.get_source_max_turn_number("empty-id") is None

    def test_none_for_empty_id(self, setup_env):
        from tools.dashboard.dao import dashboard_db as ddb
        assert ddb.get_source_max_turn_number("") is None
        assert ddb.get_source_max_turn_number(None) is None  # type: ignore[arg-type]

    def test_distinct_from_entry_count(self, setup_env):
        """Live-witness scenario: entry_count is 12342 (JSONL line count
        including tool-use), graph turn_number is 679 (operator-visible
        text turns). The two MUST NOT be conflated at the envelope —
        proven here by storing wildly-different values for both."""
        tmp_path, db_path, orgs_dir = setup_env
        jsonl = str(tmp_path / "wit.jsonl")
        Path(jsonl).touch()
        org_db = orgs_dir / "autonomy.db"
        _insert_org_source(
            org_db,
            source_id="wit-id",
            file_path=jsonl,
        )
        _insert_thought(org_db, source_id="wit-id", turn_number=679)
        _insert_row(
            db_path, "auto-witness",
            jsonl_path=jsonl,
            graph_source_id="wit-id",
            entry_count=12342,
        )
        from tools.dashboard.dao import dashboard_db as ddb

        # The two counters disagree by ~18× — exactly the live evidence
        # that motivated this bead.
        assert ddb.get_source_max_turn_number("wit-id") == 679
        assert ddb.get_session("auto-witness")["entry_count"] == 12342


# ══════════════════════════════════════════════════════════════════════
# /api/session/{tmux_name} — session detail endpoint surfaces reconciled ID
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def server_app(setup_env, tmp_path, monkeypatch):
    """Boot the dashboard server bound to the per-test dashboard.db."""
    monkeypatch.setenv("DASHBOARD_EVENT_BUS_STATE", str(tmp_path / "ebs.state"))
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    from tools.dashboard import server
    importlib.reload(server)
    return server


class TestSessionDetailReconciles:
    """Bead acceptance: ``/api/session/{tmux_name}`` MUST surface the
    reconciled graph_source_id, not the drifted stored value."""

    def test_drifted_stored_id_surfaces_reconciled(self, setup_env, server_app):
        from starlette.testclient import TestClient

        tmp_path, db_path, orgs_dir = setup_env
        jsonl = str(tmp_path / "live.jsonl")
        Path(jsonl).touch()
        _insert_org_source(
            orgs_dir / "autonomy.db",
            source_id="real-source-id-zzzz",
            file_path=jsonl,
        )
        _insert_row(
            db_path, "auto-0428-005821",
            jsonl_path=jsonl,
            graph_source_id="d15ca523-643e-drifted",  # the live evidence
        )

        with TestClient(server_app.app) as client:
            r = client.get("/api/session/auto-0428-005821")
            assert r.status_code == 200, r.text
            body = r.json()

        assert body["graph_source_id"] == "real-source-id-zzzz", \
            f"endpoint must surface the reconciled ID; got {body['graph_source_id']!r}"

    def test_drifted_with_no_jsonl_path_returns_no_id(self, setup_env, server_app):
        """A drifted ID that can't be repaired (no jsonl_path) must not
        leak. The endpoint returns ``graph_source_id: null`` so the UI
        can render a placeholder."""
        from starlette.testclient import TestClient

        _, db_path, _ = setup_env
        _insert_row(
            db_path, "auto-orphan",
            jsonl_path=None,
            graph_source_id="bogus",
        )

        with TestClient(server_app.app) as client:
            r = client.get("/api/session/auto-orphan")
            assert r.status_code == 200, r.text
            body = r.json()

        assert body["graph_source_id"] in (None, "")


# ══════════════════════════════════════════════════════════════════════
# /api/crosstalk/send — envelope carries reconciled source_id + graph turn
# ══════════════════════════════════════════════════════════════════════


class TestCrossTalkEnvelope:
    """Bead acceptance: CrossTalk envelopes from a drifted-source session
    MUST contain the reconciled source ID *and* graph turn_number, NOT
    the drifted stored ID and entry_count."""

    def _patch_auth(self, monkeypatch, server_mod, sender: str) -> str:
        """Patch auth_db so any Bearer token resolves to ``sender`` and
        message inserts are no-ops. Returns the raw token to put on the
        Authorization header.

        ``auth_db`` uses a hard-coded process-wide DB path; mocking the
        two functions the handler calls is cleaner than redirecting the
        real DB and writing rows to it.
        """
        raw = "test-token"
        monkeypatch.setattr(
            server_mod.auth_db, "resolve_token",
            lambda token_hash: sender,
        )
        monkeypatch.setattr(
            server_mod.auth_db, "insert_message",
            lambda *args, **kwargs: 0,
        )
        return raw

    def test_envelope_carries_reconciled_source_and_graph_turn(
        self, setup_env, server_app, monkeypatch,
    ):
        """Drifted ``graph_source_id`` + entry_count=12342 should NOT
        leak into the envelope. Reconciled real source + graph
        MAX(turn_number)=679 should appear instead.

        Live-witness reproduction (auto-0428-005821 envelope from
        2026-04-30): ``source="d15ca523-…"`` ``turn="12342"`` for a
        graph that had only 679 turns.
        """
        from starlette.testclient import TestClient

        tmp_path, db_path, orgs_dir = setup_env
        jsonl = str(tmp_path / "wild.jsonl")
        Path(jsonl).touch()
        org_db = orgs_dir / "autonomy.db"
        _insert_org_source(
            org_db,
            source_id="a5134fd1-7c42-real",
            file_path=jsonl,
        )
        # Graph has 679 operator-visible turns.
        _insert_thought(org_db, source_id="a5134fd1-7c42-real", turn_number=679)
        # Dashboard.db has the drifted UUID + the bigger entry_count.
        _insert_row(
            db_path, "auto-sender",
            jsonl_path=jsonl,
            graph_source_id="d15ca523-643e-drifted",
            entry_count=12342,
            label="Sender Session",
        )
        _insert_row(
            db_path, "auto-target",
            jsonl_path=str(tmp_path / "tgt.jsonl"),
            graph_source_id="",
        )

        from tools.dashboard import server as server_mod
        importlib.reload(server_mod)

        token = self._patch_auth(monkeypatch, server_mod, "auto-sender")

        # Capture the envelope tmux_send would dispatch.
        captured: dict = {}

        async def fake_tmux_send(target, payload):
            captured["target"] = target
            captured["payload"] = payload

        monkeypatch.setattr(server_mod, "tmux_send", fake_tmux_send)
        monkeypatch.setattr(
            server_mod, "_tmux_session_exists", lambda name: True,
        )

        with TestClient(server_mod.app) as client:
            r = client.post(
                "/api/crosstalk/send",
                json={"target": "auto-target", "message": "hi"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r.status_code == 200, r.text
            body = r.json()

        assert "payload" in captured, "tmux_send was never invoked"
        envelope = captured["payload"]

        # The envelope's source MUST be the reconciled ID.
        m = re.search(r'source="([^"]*)"', envelope)
        assert m, f"envelope missing source attr: {envelope!r}"
        assert m.group(1) == "a5134fd1-7c42-real", \
            f"envelope leaked drifted source; got {m.group(1)!r}"

        # The envelope's turn MUST be the graph turn_number, not entry_count.
        m_turn = re.search(r'turn="([^"]*)"', envelope)
        assert m_turn, f"envelope missing turn attr: {envelope!r}"
        assert m_turn.group(1) == "679", \
            f"envelope used wrong turn counter; got {m_turn.group(1)!r}"
        assert "12342" not in envelope, \
            "envelope must not carry entry_count"

        # API response mirrors the envelope.
        assert body["source_id"] == "a5134fd1-7c42-real"
        assert body["turn"] == 679

    def test_envelope_omits_turn_when_graph_unaware(
        self, setup_env, server_app, monkeypatch,
    ):
        """If the source is not yet ingested, MAX(turn_number) is
        unavailable — the envelope MUST emit an empty ``turn=""`` rather
        than fall back to entry_count (which is on a different scale).
        """
        from starlette.testclient import TestClient

        tmp_path, db_path, _ = setup_env
        # No org DB rows — source is unknown.
        _insert_row(
            db_path, "auto-pre-ingest",
            jsonl_path=str(tmp_path / "pending.jsonl"),
            graph_source_id="",
            entry_count=999,
        )
        _insert_row(
            db_path, "auto-target",
            jsonl_path=str(tmp_path / "tgt.jsonl"),
            graph_source_id="",
        )

        from tools.dashboard import server as server_mod
        importlib.reload(server_mod)
        token = self._patch_auth(monkeypatch, server_mod, "auto-pre-ingest")

        captured: dict = {}

        async def fake_tmux_send(target, payload):
            captured["payload"] = payload

        monkeypatch.setattr(server_mod, "tmux_send", fake_tmux_send)
        monkeypatch.setattr(server_mod, "_tmux_session_exists", lambda name: True)

        with TestClient(server_mod.app) as client:
            r = client.post(
                "/api/crosstalk/send",
                json={"target": "auto-target", "message": "hi"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r.status_code == 200, r.text

        envelope = captured["payload"]
        # turn="" — present (parser regex requires it) but empty.
        m_turn = re.search(r'turn="([^"]*)"', envelope)
        assert m_turn and m_turn.group(1) == "", \
            f"envelope must not lie about turn; got {m_turn.group(1) if m_turn else None!r}"
        # entry_count must not leak as a fallback.
        assert "999" not in envelope

    def test_send_allows_ordinary_angle_brackets_in_body(
        self, setup_env, server_app, monkeypatch,
    ):
        """Angle brackets in code snippets are allowed; only literal envelope escapes are blocked."""
        from starlette.testclient import TestClient

        tmp_path, db_path, orgs_dir = setup_env
        jsonl = str(tmp_path / "code.jsonl")
        Path(jsonl).touch()
        org_db = orgs_dir / "autonomy.db"
        _insert_org_source(
            org_db,
            source_id="code-src",
            file_path=jsonl,
        )
        _insert_row(
            db_path, "auto-sender",
            jsonl_path=jsonl,
            graph_source_id="code-src",
        )
        _insert_row(
            db_path, "auto-target",
            jsonl_path=str(tmp_path / "tgt.jsonl"),
            graph_source_id="",
        )

        from tools.dashboard import server as server_mod
        importlib.reload(server_mod)
        token = self._patch_auth(monkeypatch, server_mod, "auto-sender")

        captured: dict = {}

        async def fake_tmux_send(target, payload):
            captured["target"] = target
            captured["payload"] = payload

        monkeypatch.setattr(server_mod, "tmux_send", fake_tmux_send)
        monkeypatch.setattr(server_mod, "_tmux_session_exists", lambda name: True)

        message = "if (left < right && total > 0) return items[i];"
        with TestClient(server_mod.app) as client:
            r = client.post(
                "/api/crosstalk/send",
                json={"target": "auto-target", "message": message},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r.status_code == 200, r.text

        assert captured["target"] == "auto-target"
        assert message in captured["payload"]


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
