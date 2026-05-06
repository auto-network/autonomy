"""Tests for the session-chip enrichment on graph-source API responses.

Closes the inverse-lookup gap reported as a follow-up to the tmux-name
acceptance work: the source viewer / `graph read` need to know the tmux
session a session-type source belongs to so they can render a chip
linking to ``/session/<tmux>``. The dashboard attaches this on the
server side via ``_attach_source_session_chip`` / DAO
``get_tmux_name_for_source``; both surfaces (web + CLI) read the same
``tmux_session`` field.
"""

from __future__ import annotations

import sqlite3
import time
from unittest.mock import patch

from starlette.testclient import TestClient

import pytest


_SESSION_SOURCE = {
    "id": "abcdef0123456789cafe",
    "title": "linked session",
    "type": "session",
    "project": "autonomy",
    "created_at": "2026-05-06T10:00:00Z",
    "metadata": "{}",
}


def _seed_tmux_link(db_path: str, *, tmux_name: str, source_id: str,
                    session_uuid: str | None = None) -> None:
    """Insert a tmux_sessions row that points at a graph source."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR REPLACE INTO tmux_sessions"
        " (tmux_name, type, project, created_at, is_live,"
        "  graph_source_id, session_uuid)"
        " VALUES (?,?,?,?,1,?,?)",
        (tmux_name, "container", "autonomy", time.time(),
         source_id, session_uuid),
    )
    conn.commit()
    conn.close()


def test_get_tmux_name_for_source_by_graph_source_id(test_app, test_db):
    """Reverse lookup: graph_source_id → tmux_name."""
    _seed_tmux_link(test_db, tmux_name="auto-linked",
                    source_id=_SESSION_SOURCE["id"])
    from tools.dashboard.dao import dashboard_db
    assert dashboard_db.get_tmux_name_for_source(
        _SESSION_SOURCE["id"]
    ) == "auto-linked"


def test_get_tmux_name_for_source_falls_back_to_session_uuid(test_app, test_db):
    """When graph_source_id link hasn't landed yet, session_uuid is used."""
    _seed_tmux_link(
        test_db, tmux_name="auto-pending",
        source_id="",
        session_uuid="deadbeef-uuid",
    )
    from tools.dashboard.dao import dashboard_db
    assert dashboard_db.get_tmux_name_for_source(
        "freshsource", session_uuid="deadbeef-uuid",
    ) == "auto-pending"


def test_api_graph_attaches_tmux_session_for_session_source(test_app, test_db):
    """``GET /api/graph/{id}`` returns a ``source.tmux_session`` field for
    session-type sources so the viewer can render a chip without a second
    round trip."""
    _seed_tmux_link(test_db, tmux_name="auto-linked",
                    source_id=_SESSION_SOURCE["id"])

    from tools.dashboard import server

    def fake_read_source_full(source_id, **kwargs):
        return {
            "source": dict(_SESSION_SOURCE),
            "entries": [],
            "truncated": False,
            "total_chars": 0,
            "comments": [],
        }

    async def passthrough_refresh(source):
        return source

    with patch.object(server.graph_ops, "get_source",
                      return_value=dict(_SESSION_SOURCE)):
        with patch.object(server.graph_ops, "read_source_full",
                          side_effect=fake_read_source_full):
            with patch.object(server, "_refresh_graph_session_source",
                              passthrough_refresh):
                with TestClient(test_app) as client:
                    r = client.get(f"/api/graph/{_SESSION_SOURCE['id']}")
                    assert r.status_code == 200, r.text
                    body = r.json()

    assert body["source"]["tmux_session"] == "auto-linked"


def test_attach_chip_skips_non_session_sources(test_app, test_db):
    """Non-session sources never get a ``tmux_session`` field — the chip
    is only rendered for ``type=session``."""
    _seed_tmux_link(test_db, tmux_name="auto-linked",
                    source_id=_SESSION_SOURCE["id"])

    note = dict(_SESSION_SOURCE, type="note")
    from tools.dashboard import server

    server._attach_source_session_chip(note)
    assert "tmux_session" not in note


def test_attach_chip_silent_on_unlinked_session(test_app, test_db):
    """Sessions with no dashboard-db row are left untouched (no key
    inserted) so the viewer simply doesn't render the chip."""
    from tools.dashboard import server

    src = dict(_SESSION_SOURCE, id="ffffffffffffffffffff")
    server._attach_source_session_chip(src)
    assert "tmux_session" not in src
