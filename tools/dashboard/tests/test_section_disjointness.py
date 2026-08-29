"""THE origin-bug test: a session appears in exactly one dashboard section.

The 2026-07-09 double-display screenshots (same session in Active with a
green Live badge AND in Recent with a Resume button) happened because the
two feeds — Active (the live registry) and Recent (graph rows minus live
sessions) — could disagree about liveness when the legacy columns drifted.
The FSM consolidation makes them agree by construction; this test drives
BOTH REAL FEEDS with sessions in every lifecycle state and asserts the
sections are disjoint AND total. It must survive Phase D: the Recent
feed's live-exclusion currently keys on the legacy ``is_live`` column via
the write-through — when D drops that column, this test is what catches
the filter not having been flipped to ``state``.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tools.dashboard.dao import dashboard_db
from tools.dashboard.dao import sessions as dao_sessions
from tools.dashboard.session_lifecycle_worker import STATE_AUTHORITY

ALL_STATES = ["LAUNCHING", "ACTIVE", "STOPPING", "ENDED", "FAILED"]


@pytest.fixture
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "dashboard.db"
    monkeypatch.setenv("DASHBOARD_DB", str(db_path))
    prior = getattr(dashboard_db, "_conn", None)
    if prior is not None:
        try:
            prior.close()
        except Exception:
            pass
    dashboard_db._conn = None  # type: ignore[attr-defined]
    dashboard_db._DB_PATH = db_path  # type: ignore[attr-defined]
    conn = dashboard_db.get_conn()
    yield conn
    try:
        conn.close()
    except Exception:
        pass
    dashboard_db._conn = None  # type: ignore[attr-defined]


def _seed_all_states(tmp_path) -> dict[str, str]:
    """One session per lifecycle state, each with a uuid + real jsonl file."""
    names = {}
    for state in ALL_STATES:
        name = f"auto-{state.lower()}"
        names[state] = name
        jsonl = tmp_path / f"{name}.jsonl"
        jsonl.write_text('{"type": "user"}\n')
        dashboard_db.insert_session(
            tmux_name=name,
            session_type="container",
            project="autonomy",
            session_uuid=f"uuid-{name}",
            jsonl_path=str(jsonl),
            state="LAUNCHING",
        )
        if state == "ACTIVE":
            STATE_AUTHORITY.transition(name, "ACTIVE", cause="test")
        elif state == "STOPPING":
            STATE_AUTHORITY.transition(name, "STOPPING", phase="stopping", cause="test")
        elif state == "ENDED":
            STATE_AUTHORITY.transition(name, "ACTIVE", cause="test")
            STATE_AUTHORITY.transition(name, "ENDED", cause="test")
        elif state == "FAILED":
            STATE_AUTHORITY.transition(
                name, "FAILED", cause="test", reason="x", failed_phase="setup",
            )
    return names


class _FakeGraphDb:
    """Minimal stand-in for a peer graph DB: one sources table."""

    def __init__(self, rows):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE sources (id TEXT, type TEXT, title TEXT,"
            " created_at TEXT, last_activity_at TEXT, file_path TEXT,"
            " metadata TEXT)"
        )
        for r in rows:
            self.conn.execute(
                "INSERT INTO sources VALUES (?,?,?,?,?,?,?)", r,
            )
        self.conn.commit()


def _active_feed():
    """The real Active feed: monitor registry filtered to interactive types
    (exactly what api_dao_active_sessions serves in non-mock mode)."""
    from tools.dashboard.session_monitor import SessionMonitor

    m = SessionMonitor()
    m._event_bus = AsyncMock()
    registry = m.get_registry()
    return [
        s for s in registry
        if s.get("type") in dao_sessions._ACTIVE_SESSION_TYPES
    ]


def _recent_feed(names, monkeypatch):
    """The real Recent feed with the graph-row step fed synthetic sources
    matching the seeded sessions (all of them recent candidates)."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    graph_rows = []
    for state, name in names.items():
        row = dashboard_db.get_session(name)
        graph_rows.append((
            f"src-{name}", "session", f"title {name}",
            now_iso, now_iso, row["jsonl_path"],
            json.dumps({"session_uuid": row["session_uuid"]}),
        ))
    fake_db = _FakeGraphDb(graph_rows)

    from tools.graph import cross_org
    # all_store_slugs passes root= through; a bare lambda broke this
    # tripwire silently until 2026-08-29 — accept any signature.
    monkeypatch.setattr(cross_org, "list_org_slugs", lambda *a, **kw: ["autonomy"])
    monkeypatch.setattr(cross_org, "open_peer_db", lambda slug: fake_db)
    monkeypatch.setattr(
        dao_sessions, "_claude_credentials_alias_map", lambda: {},
    )
    return dao_sessions.get_recent_sessions(since="1d")


@pytest.mark.asyncio
async def test_sections_are_disjoint_and_total(db, tmp_path, monkeypatch):
    names = _seed_all_states(tmp_path)

    active = {s["session_id"] for s in _active_feed()}
    recent = {
        r["tmux_session"] for r in _recent_feed(names, monkeypatch)
        if r.get("tmux_session")
    }

    # Disjoint: no session may appear in both sections — the double-display
    # of 2026-07-09 is exactly a non-empty intersection here.
    assert active & recent == set(), (
        f"session(s) in BOTH sections: {active & recent}"
    )
    # Placement: non-terminal states are Active; terminal states are Recent.
    assert active == {names["LAUNCHING"], names["ACTIVE"], names["STOPPING"]}
    assert recent == {names["ENDED"], names["FAILED"]}
    # Total: every seeded session is visible somewhere.
    assert active | recent == set(names.values())


@pytest.mark.asyncio
async def test_terminal_states_never_reach_the_active_feed(db, tmp_path, monkeypatch):
    """With the legacy columns dropped, state alone decides the sections —
    a terminal row cannot reach the Active feed through any field."""
    names = _seed_all_states(tmp_path)
    active = {s["session_id"] for s in _active_feed()}
    assert names["ENDED"] not in active
    assert names["FAILED"] not in active
