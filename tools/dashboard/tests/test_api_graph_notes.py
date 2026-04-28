"""Tests for /api/graph/notes — bead auto-yn1gt.

The /collab "Recent" tab needs notes regardless of tag, ordered by
``created_at`` DESC, optionally filtered by ``since`` / ``tags`` / ``only_org``.
The endpoint is a thin wrapper over ``graph_ops.list_notes`` that flattens
the row metadata into the shape the Alpine view expects.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from starlette.testclient import TestClient


# Cross-org fixture: rows from two orgs, mixed tags, descending time.
FIXTURE_ROWS = [
    {
        "id": "src-anchore-001",
        "title": "Anchore architecture note",
        "type": "note",
        "project": "anchore",
        "org": "anchore",
        "created_at": "2026-04-27T14:00:00Z",
        "metadata": json.dumps({
            "author": "agent-anchore",
            "tags": ["architecture", "auth"],
        }),
    },
    {
        "id": "src-autonomy-001",
        "title": "pitfall: dashboard hot-reload",
        "type": "note",
        "project": "autonomy",
        "org": "autonomy",
        "created_at": "2026-04-26T10:00:00Z",
        "metadata": json.dumps({
            "author": "host-0418-192255",
            "tags": ["pitfall", "dashboard"],
        }),
    },
    {
        "id": "src-autonomy-002",
        "title": "Per-org DB architecture",
        "type": "note",
        "project": "autonomy",
        "org": "autonomy",
        "created_at": "2026-04-25T09:30:00Z",
        "metadata": {
            "author": "terminal:host-0420-122533",
            "tags": ["architecture", "graph"],
        },
    },
    {
        "id": "src-anchore-002",
        "title": "Anchore release notes",
        "type": "note",
        "project": "anchore",
        "org": "anchore",
        "created_at": "2026-04-24T08:00:00Z",
        "metadata": json.dumps({
            "author": "release-bot",
            "tags": ["release"],
        }),
    },
]


def _filter_rows(*, only_org=None, since=None, tags=None, limit=50):
    """Mirror the subset of ``list_notes`` semantics this endpoint relies on
    so each test asserts the *endpoint's* behavior without exercising the
    real per-org SQLite stack."""
    rows = list(FIXTURE_ROWS)
    if only_org:
        rows = [r for r in rows if r["org"] == only_org]
    if since:
        rows = [r for r in rows if r["created_at"] >= since]
    if tags:
        def _tags(r):
            m = r["metadata"]
            if isinstance(m, str):
                m = json.loads(m)
            return m.get("tags", [])
        rows = [r for r in rows if all(t in _tags(r) for t in tags)]
    rows.sort(key=lambda r: r["created_at"], reverse=True)
    return rows[:limit]


def _patched_list_notes(test_app, monkeypatch):
    from tools.dashboard import server

    def fake_list_notes(*, org=None, only_org=None, since=None, tags=None, limit=50, **kw):
        return _filter_rows(only_org=only_org, since=since, tags=tags, limit=limit)

    return patch.object(
        server.graph_ops, "list_notes", staticmethod(fake_list_notes), create=True,
    )


def test_api_graph_notes_returns_recent(test_app):
    """Default GET returns notes ordered by created_at DESC, capped at limit."""
    with _patched_list_notes(test_app, None):
        with TestClient(test_app) as client:
            r = client.get("/api/graph/notes?limit=2")
    assert r.status_code == 200
    body = r.json()
    assert "notes" in body
    notes = body["notes"]
    assert len(notes) == 2
    # Newest first.
    assert notes[0]["id"] == "src-anchore-001"
    assert notes[1]["id"] == "src-autonomy-001"


def test_api_graph_notes_filters_by_since(test_app):
    """``?since=24h`` excludes rows older than the cutoff."""
    from tools.dashboard import server

    captured = {}

    def fake_list_notes(*, org=None, only_org=None, since=None, tags=None, limit=50, **kw):
        captured["since"] = since
        return _filter_rows(only_org=only_org, since=since, tags=tags, limit=limit)

    with patch.object(server.graph_ops, "list_notes",
                      staticmethod(fake_list_notes), create=True):
        with TestClient(test_app) as client:
            r = client.get("/api/graph/notes?since=24h&limit=20")
    assert r.status_code == 200
    # ``_parse_range`` must have produced a non-None cutoff that the handler
    # forwarded to graph_ops. The exact value is wall-clock dependent, so
    # we just assert it was passed through.
    assert captured["since"] is not None
    assert isinstance(captured["since"], str)


def test_api_graph_notes_filters_by_tags(test_app):
    """``?tags=pitfall`` returns only pitfall-tagged sources."""
    with _patched_list_notes(test_app, None):
        with TestClient(test_app) as client:
            r = client.get("/api/graph/notes?tags=pitfall")
    assert r.status_code == 200
    notes = r.json()["notes"]
    assert len(notes) == 1
    assert notes[0]["id"] == "src-autonomy-001"
    assert "pitfall" in notes[0]["tags"]


def test_api_graph_notes_only_org_pin(test_app):
    """``?only_org=anchore`` pins to one org's notes."""
    with _patched_list_notes(test_app, None):
        with TestClient(test_app) as client:
            r = client.get("/api/graph/notes?only_org=anchore&limit=10")
    assert r.status_code == 200
    notes = r.json()["notes"]
    assert len(notes) == 2
    assert {n["id"] for n in notes} == {"src-anchore-001", "src-anchore-002"}
    assert all(n["org"] == "anchore" for n in notes)


def test_api_graph_notes_includes_source_type_and_org(test_app):
    """Every returned note has ``source_type`` and ``org`` populated."""
    with _patched_list_notes(test_app, None):
        with TestClient(test_app) as client:
            r = client.get("/api/graph/notes?limit=10")
    assert r.status_code == 200
    notes = r.json()["notes"]
    assert notes, "expected at least one note"
    for n in notes:
        assert "source_type" in n
        # Note rows come back as ``type='note'`` from list_sources; the
        # handler maps that into ``source_type``.
        assert n["source_type"] == "note"
        assert "org" in n
        assert n["org"] in ("anchore", "autonomy")
        # Author + tags survive the metadata flatten.
        assert "author" in n
        assert "tags" in n
        assert isinstance(n["tags"], list)
