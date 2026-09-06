"""``/api/search?source_ids=`` — restrict a search to named sources.

The sessions page sends the graph source ids of every card on screen so the
header search box can act as an in-place filter. Contracts:

1. The ids are forwarded to ``ops.search`` as ``only_source_ids`` (deduped,
   whitespace-stripped).
2. Each listed session source is refreshed from its JSONL *before* the
   search runs, because live sessions are only indexed on demand.
3. A present-but-empty list is a 400, never a silent widen to the whole
   graph; an oversized list is a 400 too.
4. Without the parameter nothing changes (``only_source_ids=None``, no
   refresh).
"""

from __future__ import annotations

from unittest.mock import patch

from starlette.testclient import TestClient


def test_source_ids_forwarded_and_refreshed(test_app):
    from tools.dashboard import server

    captured: dict = {}
    refreshed: list = []

    def fake_search(q, **kwargs):
        captured["q"] = q
        captured.update(kwargs)
        return []

    async def fake_refresh(ids, *, org):
        refreshed.append((list(ids), org))

    with patch.object(server.graph_ops, "search", side_effect=fake_search), \
         patch.object(server, "_refresh_graph_session_sources", side_effect=fake_refresh):
        with TestClient(test_app) as client:
            r = client.get("/api/search?q=rollback&group=1&source_ids=src-a,%20src-b,src-a,,src-c")
            assert r.status_code == 200

    assert captured["q"] == "rollback"
    assert captured["only_source_ids"] == ["src-a", "src-b", "src-c"]
    assert refreshed == [(["src-a", "src-b", "src-c"], None)]


def test_source_ids_absent_is_a_noop(test_app):
    from tools.dashboard import server

    captured: dict = {}
    refreshed: list = []

    def fake_search(q, **kwargs):
        captured.update(kwargs)
        return []

    async def fake_refresh(ids, *, org):
        refreshed.append(ids)

    with patch.object(server.graph_ops, "search", side_effect=fake_search), \
         patch.object(server, "_refresh_graph_session_sources", side_effect=fake_refresh):
        with TestClient(test_app) as client:
            assert client.get("/api/search?q=rollback").status_code == 200

    assert captured["only_source_ids"] is None
    assert refreshed == []


def test_source_ids_empty_or_oversized_is_400(test_app):
    from tools.dashboard import server

    with patch.object(server.graph_ops, "search", return_value=[]) as search:
        with TestClient(test_app) as client:
            r = client.get("/api/search?q=x&source_ids=")
            assert r.status_code == 400
            assert "source_ids" in r.json()["error"]

            r = client.get("/api/search?q=x&source_ids=,%20,")
            assert r.status_code == 400

            too_many = ",".join(f"src-{i}" for i in range(server._SEARCH_MAX_SOURCE_IDS + 1))
            r = client.get("/api/search?q=x&source_ids=" + too_many)
            assert r.status_code == 400
            assert "maximum" in r.json()["error"]
    assert search.call_count == 0


def test_refresh_helper_skips_non_sessions_and_failures(test_app):
    """One bad id must not block the others or the search."""
    import asyncio
    from tools.dashboard import server

    seen: list = []

    def fake_get_source(source_id, *, org=None, peers=None):
        if source_id == "missing":
            return None
        if source_id == "boom":
            raise RuntimeError("db exploded")
        return {"id": source_id, "type": "session" if source_id.startswith("sess") else "note"}

    def fake_refresh(source):
        seen.append(source["id"])
        return source

    with patch.object(server.graph_ops, "get_source", side_effect=fake_get_source), \
         patch("tools.graph.ingest.refresh_session_source", side_effect=fake_refresh):
        asyncio.run(server._refresh_graph_session_sources(
            ["sess-1", "missing", "note-1", "boom", "sess-2"], org=None,
        ))

    assert seen == ["sess-1", "sess-2"]
