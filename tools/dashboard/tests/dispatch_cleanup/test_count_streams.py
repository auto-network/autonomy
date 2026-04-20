"""auto-8bnq0 Defect 4 — _count_streams() SQL ambiguity.

The query at tools/dashboard/server.py::_count_streams joins the `sources`
table with `json_each(...)` in a comma-join and filters `WHERE type =
'note'`. Both the `sources` table and `json_each`'s output schema expose
a `type` column, so SQLite raises:

  OperationalError: ambiguous column name: type

Fires on every dispatch_watcher tick (~5s), spamming dashboard.log. The
nav "Streams" badge falls back to empty as a side effect.

FAIL-REASON on master: calling _count_streams() raises OperationalError.

Fix: qualify the column (sources.type, or an aliased join).
"""
from __future__ import annotations

import sqlite3
import sys
import types

import pytest


class TestCountStreamsNoAmbiguousColumn:
    """Defect 4 — _count_streams must not raise on a realistic schema."""

    def test_count_streams_no_ambiguous_column(self, tmp_path, monkeypatch):
        """_count_streams() executes cleanly against a graph.db with notes."""
        graph_db = tmp_path / "graph.db"
        conn = sqlite3.connect(str(graph_db))
        # Minimal sources schema matching the production shape the query
        # needs to understand. 'type' is the offending column.
        conn.execute(
            """
            CREATE TABLE sources (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                title TEXT,
                metadata TEXT DEFAULT '{}'
            )
            """
        )
        # Seed a couple of notes with tag streams to make the count non-trivial
        conn.execute(
            "INSERT INTO sources (id, type, title, metadata) VALUES "
            "('src-a', 'note', 'A', '{\"tags\":[\"pitfall\",\"dispatcher\"]}'),"
            "('src-b', 'note', 'B', '{\"tags\":[\"pitfall\",\"session-monitor\"]}'),"
            "('src-c', 'doc', 'C',  '{\"tags\":[\"unrelated\"]}')"
        )
        conn.commit()
        conn.close()

        # Point the server at this graph.db. The production helper uses
        # _graph_db_path() to resolve; override via env.
        monkeypatch.setenv("GRAPH_DB", str(graph_db))

        import importlib
        from tools.dashboard import server as srvmod
        importlib.reload(srvmod)

        try:
            count = srvmod._count_streams()
        except sqlite3.OperationalError as exc:
            pytest.fail(
                f"_count_streams raised OperationalError: {exc}. "
                "The SQL has ambiguous column 'type' (both sources.type and "
                "json_each.type are in scope). Qualify with a table alias."
            )
        except Exception as exc:
            pytest.fail(
                f"_count_streams raised unexpected {type(exc).__name__}: {exc}"
            )

        # Sanity: with 3 distinct note-tag values (pitfall, dispatcher,
        # session-monitor) the count should be 3. 'unrelated' comes from
        # a doc, not a note, so it's excluded.
        assert count == 3, (
            f"_count_streams returned {count}; expected 3 (pitfall, "
            "dispatcher, session-monitor — all from notes). If the query "
            "is now counting all tags across all source types, the type "
            "filter lost its effect."
        )
