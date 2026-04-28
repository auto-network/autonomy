"""Tests for the dashboard surfaces that render ``short_description``.

Bead auto-kt3z3 lifts ``short_description`` from a metadata-JSON nested
field to a first-class column. The surfaces exposed to the browser must
forward that field on GET responses (resolve / recent-notes / search) so
the source viewer + collab cards + search-result cards can render the
description without re-reading the raw metadata blob.
"""

from __future__ import annotations

from unittest.mock import patch

from starlette.testclient import TestClient


_NOTE_WITH_DESC = {
    "id": "11111111-1111-1111-1111-111111111111",
    "type": "note",
    "title": "Passkey design",
    "short_description": "Two-paragraph rationale for the passkey rollout.",
    "project": "autonomy",
    "platform": "local",
    "created_at": "2026-04-01T10:00:00Z",
    "metadata": "{}",
}


def test_api_graph_resolve_returns_short_description(test_app):
    """GET /api/graph/<id> exposes ``short_description`` on the source dict."""
    from tools.dashboard import server

    def fake_read_source_full(source_id, **kwargs):
        return {
            "source": dict(_NOTE_WITH_DESC),
            "entries": [],
            "truncated": False,
            "total_chars": 0,
            "comments": [],
        }

    with patch.object(server.graph_ops, "get_source",
                      return_value=dict(_NOTE_WITH_DESC)):
        with patch.object(server.graph_ops, "read_source_full",
                          side_effect=fake_read_source_full):
            with TestClient(test_app) as client:
                r = client.get(f"/api/graph/{_NOTE_WITH_DESC['id']}")
                assert r.status_code == 200, r.text
                body = r.json()

    src = body.get("source")
    assert src is not None
    assert src.get("short_description") == _NOTE_WITH_DESC["short_description"]


def test_api_graph_notes_returns_short_description(test_app):
    """GET /api/graph/notes carries ``short_description`` per item."""
    from tools.dashboard import server

    rows = [
        {
            "id": _NOTE_WITH_DESC["id"],
            "title": "Passkey design",
            "short_description": "Two-paragraph rationale for the passkey rollout.",
            "type": "note",
            "project": "autonomy",
            "org": "autonomy",
            "created_at": "2026-04-01T10:00:00Z",
            "metadata": '{"author": "tester", "tags": ["auth"]}',
        },
        {
            # Older note with no description set — handler must return None,
            # not raise on the missing key.
            "id": "22222222-2222-2222-2222-222222222222",
            "title": "Older note",
            "type": "note",
            "project": "autonomy",
            "org": "autonomy",
            "created_at": "2026-03-01T10:00:00Z",
            "metadata": "{}",
        },
    ]

    def fake_list_notes(*, org=None, only_org=None, since=None, tags=None,
                        limit=50, **kw):
        return rows

    with patch.object(server.graph_ops, "list_notes",
                      staticmethod(fake_list_notes), create=True):
        with TestClient(test_app) as client:
            r = client.get("/api/graph/notes?limit=10")
            assert r.status_code == 200, r.text
            body = r.json()

    notes = body["notes"]
    assert len(notes) == 2
    by_id = {n["id"]: n for n in notes}
    first = by_id[_NOTE_WITH_DESC["id"]]
    assert first["short_description"] == _NOTE_WITH_DESC["short_description"]
    # Missing description on older notes comes back as None, not absent —
    # the front-end uses an `x-if` truthiness check, so either renders fine.
    second = by_id["22222222-2222-2222-2222-222222222222"]
    assert "short_description" in second
    assert second["short_description"] is None


def test_api_search_results_include_short_description(test_app):
    """Grouped /api/search rows lift ``short_description`` to the group dict
    so the search card can render it under the title."""
    from tools.dashboard import server

    rows = [
        {
            "source_id": _NOTE_WITH_DESC["id"],
            "source_title": "Passkey design",
            "source_type": "note",
            "source_created_at": "2026-04-01T10:00:00Z",
            "source_metadata": "{}",
            "short_description": _NOTE_WITH_DESC["short_description"],
            "project": "autonomy",
            "platform": "local",
            "id": "thought-1",
            "result_type": "thought",
            "turn_number": 12,
            "content": "matching turn excerpt",
            "rank": -9.5,
        },
    ]

    with patch.object(server.graph_ops, "search", return_value=rows):
        with TestClient(test_app) as client:
            r = client.get("/api/search?q=passkey&group=1")
            assert r.status_code == 200, r.text
            body = r.json()

    assert isinstance(body, list)
    assert len(body) == 1
    group = body[0]
    assert group["source_id"] == _NOTE_WITH_DESC["id"]
    assert group.get("short_description") == _NOTE_WITH_DESC["short_description"]


def test_note_viewer_template_renders_short_description(test_app):
    """The /pages/source fragment exposes the short_description binding so
    the JS view can render it under the title.

    We don't run the Alpine runtime in this test — instead we assert the
    template carries the binding the renderer expects (``shortDescription``
    + the ``data-testid`` hook). If the binding regresses, the tag-based
    assertion fails fast.
    """
    with TestClient(test_app) as client:
        r = client.get("/pages/source")
        assert r.status_code == 200, r.text
        html = r.text

    assert 'data-testid="sc-short-description"' in html
    assert 'x-text="shortDescription"' in html
