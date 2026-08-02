"""Tests for /api/search grouping + query-string passthrough (auto-lpzcc).

The dashboard search endpoint must:

1. Preserve per-turn excerpts when ``?group=1`` collapses duplicate sources,
   so the upcoming card UI can render multi-turn drill-downs.
2. Lift ``source_type`` (and other source-level fields) from the underlying
   FTS rows into the group dict so cards can colour by note vs session, etc.
3. Forward ``tag``, ``states``, ``include_raw``, ``only_org``, and ``peers``
   from query string through to ``ops.search`` — the structured search
   endpoint already does this, the legacy one didn't.
"""

from __future__ import annotations

from unittest.mock import patch

from starlette.testclient import TestClient


def _make_excerpt_rows():
    """Two FTS rows for the same source, different turns, same shape that
    ``GraphDB.search`` would produce after the source_type/source_created_at
    column add."""
    base = {
        "source_id": "src-aaa",
        "source_title": "Pitfall doc",
        "source_type": "note",
        "source_created_at": "2026-03-22T14:32:00Z",
        "source_metadata": "{}",
        "project": "autonomy",
        "platform": "local",
    }
    return [
        {
            **base,
            "id": "thought-1",
            "result_type": "thought",
            "turn_number": 649,
            "content": "first matching turn excerpt",
            "rank": -9.98,
        },
        {
            **base,
            "id": "thought-2",
            "result_type": "thought",
            "turn_number": 933,
            "content": "second matching turn excerpt",
            "rank": -9.64,
        },
    ]


def test_grouped_search_preserves_excerpts(test_app):
    """``?group=1`` collapses by source but keeps every turn-level excerpt."""
    from tools.dashboard import server

    rows = _make_excerpt_rows()
    with patch.object(server.graph_ops, "search", return_value=rows):
        with TestClient(test_app) as client:
            r = client.get("/api/search?q=pitfall&group=1")
            assert r.status_code == 200
            body = r.json()

    assert isinstance(body, list)
    assert len(body) == 1
    group = body[0]
    assert group["match_count"] == 2
    assert "excerpts" in group
    turns = sorted(e.get("turn_number") for e in group["excerpts"])
    assert turns == [649, 933]
    # Each excerpt carries the per-turn fields.
    for e in group["excerpts"]:
        assert "content" in e
        assert "result_type" in e
        assert "rank" in e


def test_grouped_search_pulls_source_type_to_group(test_app):
    """Source-level fields (``source_type``, ``source_title``) propagate up
    to the group dict so the card chrome can render without re-reading the
    excerpts array."""
    from tools.dashboard import server

    rows = _make_excerpt_rows()
    with patch.object(server.graph_ops, "search", return_value=rows):
        with TestClient(test_app) as client:
            r = client.get("/api/search?q=pitfall&group=1")
            assert r.status_code == 200
            body = r.json()

    group = body[0]
    assert group["source_type"] == "note"
    assert group["source_title"] == "Pitfall doc"
    assert group["source_id"] == "src-aaa"
    # The group's best (lowest) rank surfaces; -9.98 < -9.64.
    assert group["rank"] == -9.98


def test_search_passes_through_only_org_tag_states(test_app):
    """The legacy ``/api/search`` endpoint must forward the cross-org and
    state-filter knobs that the structured endpoint already supports."""
    from tools.dashboard import server

    captured: dict = {}

    def fake_search(q, **kwargs):
        captured["q"] = q
        captured.update(kwargs)
        return []

    with patch.object(server.graph_ops, "search", side_effect=fake_search):
        with TestClient(test_app) as client:
            r = client.get(
                "/api/search?q=x"
                "&only_org=anchore"
                "&peers=anchore,autonomy"
                "&tag=pitfall"
                "&states=published,canonical"
                "&include_raw=1"
            )
            assert r.status_code == 200

    assert captured["q"] == "x"
    assert captured.get("only_org") == "anchore"
    assert captured.get("peers") == ["anchore", "autonomy"]
    assert captured.get("tag") == "pitfall"
    assert captured.get("states") == ["published", "canonical"]
    assert captured.get("include_raw") is True


def test_search_passes_through_ranker(test_app):
    """The dashboard comparison control reaches ``graph_ops.search``."""
    from tools.dashboard import server

    captured: dict = {}

    def fake_search(q, **kwargs):
        captured["q"] = q
        captured.update(kwargs)
        return []

    with patch.object(server.graph_ops, "search", side_effect=fake_search):
        with TestClient(test_app) as client:
            response = client.get("/api/search?q=ranking&ranker=smart")

    assert response.status_code == 200
    assert captured["q"] == "ranking"
    assert captured["ranker"] == "smart"


def test_search_rejects_unknown_ranker(test_app):
    with TestClient(test_app) as client:
        response = client.get("/api/search?q=ranking&ranker=surprise")

    assert response.status_code == 400
    assert "invalid ranker" in response.json()["error"]
