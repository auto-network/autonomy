"""Tests for the v3 search results page (auto-bcxdr).

The page is an Alpine.js template — most rendering happens client-side. These
tests assert two things together:

1. The static template fragment served by ``/pages/search`` has the right
   scaffolding (chip rail container, results container, source-card template,
   accent rail bound to ``source_type`` not ``result_type``).
2. The ``/api/search?...&group=1`` response carries the per-source shape the
   page consumes: ``source_type``, ``source_title``, ``match_count``,
   ``excerpts`` array — so the chip rail / accent rail / multi-hit excerpt
   list will render correctly when Alpine boots.

A separate browser smoke test (``test_search_smoke.py``) exercises the
full client render at iPhone width.
"""

from __future__ import annotations

import os
from unittest.mock import patch

from starlette.testclient import TestClient


# ── Fixtures ──────────────────────────────────────────────────────────


def _grouped_fixture_rows():
    """Mock FTS rows representing four source types with a multi-hit session."""
    base_session = {
        "source_id": "src-session-1",
        "source_title": "Dashboard search rework conversation",
        "source_type": "session",
        "source_created_at": "2026-04-20T03:14:58Z",
        "project": "autonomy",
        "platform": "claude-code",
    }
    base_note = {
        "source_id": "src-note-1",
        "source_title": "pitfall: dashboard search regression",
        "source_type": "note",
        "source_created_at": "2026-04-14T22:10:02Z",
        "project": "autonomy",
        "platform": "local",
    }
    base_agent = {
        "source_id": "src-agent-1",
        "source_title": "Graph search: resolve source ID queries",
        "source_type": "agent-run",
        "source_created_at": "2026-04-12T08:00:00Z",
        "project": "autonomy",
        "platform": "claude-code",
    }
    base_docs = {
        "source_id": "src-docs-1",
        "source_title": "Search Results & Graph Viewer brief",
        "source_type": "docs",
        "source_created_at": "2026-03-23T10:00:00Z",
        "project": "autonomy",
        "platform": "local",
    }
    return [
        # Multi-hit session: same source_id, different turn_numbers.
        {**base_session, "id": "thought-1", "result_type": "thought",
         "turn_number": 649, "content": "first matching turn excerpt", "rank": -9.98},
        {**base_session, "id": "thought-2", "result_type": "thought",
         "turn_number": 933, "content": "second matching turn excerpt", "rank": -9.64},
        # Single-hit note (source-level, no turn).
        {**base_note, "id": "note-1", "result_type": "thought",
         "turn_number": None, "content": "Note body content with the query term inline", "rank": -7.0},
        # Single-hit agent run (with a turn).
        {**base_agent, "id": "agent-1", "result_type": "derivation",
         "turn_number": 17, "content": "agent run turn excerpt", "rank": -6.5},
        # Single-hit docs row.
        {**base_docs, "id": "docs-1", "result_type": "thought",
         "turn_number": None, "content": "docs paragraph excerpt", "rank": -6.0},
    ]


def _patch_search(monkey_target, rows):
    """Helper: patch graph_ops.search to return the supplied rows."""
    return patch.object(monkey_target, "search", return_value=rows)


# ── 1. Template scaffolding ───────────────────────────────────────────


def test_search_page_renders_with_grouped_fixture(test_app):
    """``/pages/search`` returns the v3 fragment with chip rail, results
    container, and a source-card x-for binding."""
    with TestClient(test_app) as client:
        r = client.get("/pages/search")
        assert r.status_code == 200
        html = r.text

    # Alpine root component
    assert 'x-data="searchPage()"' in html
    # Chip rail container
    assert 'data-testid="sp-chip-rail"' in html
    # Results container
    assert 'data-testid="sp-results"' in html
    # Source-card template binding (one card per result)
    assert 'class="sp-source-card"' in html
    # Excerpt list rendering
    assert 'class="sp-excerpt"' in html or "sp-excerpt-text" in html


def test_search_page_exposes_ranker_comparison_control(test_app):
    """The search page offers a bookmarkable Legacy/Smart ranking lens."""
    with TestClient(test_app) as client:
        html = client.get("/pages/search").text
        script = client.get("/static/js/pages/search.js").text

    assert 'data-testid="sp-ranker-chip"' in html
    assert 'data-testid="sp-ranker-dropdown"' in html
    assert ':data-ranker-key="opt.key"' in html
    assert "selectedOrder === 'relevance'" in html
    assert "pickRanker(key)" in script
    assert "url.searchParams.set('ranker', this.selectedRanker)" in script
    assert "url += '&ranker='" in script


# ── 2. Chip rail counts ───────────────────────────────────────────────


def test_search_page_chip_rail_includes_all_source_types(test_app):
    """The grouped /api/search response carries one entry per source_type so
    the chip rail can derive non-zero counts for note, session, agent-run,
    and docs."""
    from tools.dashboard import server

    rows = _grouped_fixture_rows()
    with _patch_search(server.graph_ops, rows):
        with TestClient(test_app) as client:
            r = client.get("/api/search?q=dashboard&group=1&limit=20")
            assert r.status_code == 200
            groups = r.json()

    types_seen = {g["source_type"] for g in groups}
    assert {"note", "session", "agent-run", "docs"}.issubset(types_seen)
    # Each chip would render a non-zero count from the result set.
    counts = {}
    for g in groups:
        counts[g["source_type"]] = counts.get(g["source_type"], 0) + 1
    for t in ("note", "session", "agent-run", "docs"):
        assert counts[t] >= 1, f"chip rail count for {t} would be zero"


# ── 3. Multi-hit excerpt list ─────────────────────────────────────────


def test_search_page_multi_hit_renders_excerpt_list(test_app):
    """A grouped result with two turn-level excerpts must surface both turn
    numbers and provide enough data for the template to wrap each in
    ``<a href="/graph/{id}?turn={n}">``."""
    from tools.dashboard import server

    rows = _grouped_fixture_rows()
    with _patch_search(server.graph_ops, rows):
        with TestClient(test_app) as client:
            r = client.get("/api/search?q=dashboard&group=1&limit=20")
            assert r.status_code == 200
            groups = r.json()

    session_group = next(g for g in groups if g["source_id"] == "src-session-1")
    assert session_group["match_count"] == 2
    excerpts = session_group["excerpts"]
    assert len(excerpts) == 2
    turns = sorted(e["turn_number"] for e in excerpts)
    assert turns == [649, 933]
    # Each excerpt has the data the template needs to build the turn-link.
    for e in excerpts:
        assert "content" in e
        assert e["turn_number"] in (649, 933)

    # Verify the template renders the per-turn anchor pattern.
    with TestClient(test_app) as client:
        page = client.get("/pages/search").text
    assert ':href="sourceHref(r, ex.turn_number)"' in page
    assert '@click.prevent.stop="navigateTo(sourceHref(r, ex.turn_number))"' in page


# ── 4. Accent rail bound to source_type, not result_type ──────────────


def test_search_page_accent_rail_by_source_type(test_app):
    """The accent rail class derives from a session_type-aware key —
    Round 7k folded ``rowPillKey()`` over the raw ``source_type`` so a
    bead-driven session paints the orange "dispatch" rail. The legacy
    template painted from ``r.result_type`` and got every card the same
    yellow rail. Guard against regressing to result_type."""
    with TestClient(test_app) as client:
        html = client.get("/pages/search").text

    # The current template binds the accent rail's class function to the
    # row pill key (session_type-aware) — NOT the raw source_type and NOT
    # the result_type.
    assert 'railClass(rowPillKey(r))' in html
    assert 'railClass(r.result_type)' not in html
    assert 'typeColor(r.result_type)' not in html

    # Per-source-type rail classes exist in the stylesheet block.
    for cls in ("sp-rail-note", "sp-rail-session", "sp-rail-agent-run",
                "sp-rail-conversation", "sp-rail-docs"):
        assert cls in html, f"missing rail class {cls!r}"


# ── 5. Mock-mode integration: ?group=1 collapses fixture rows ─────────


def test_search_page_mock_mode_groups_fixture_rows(test_app):
    """When DASHBOARD_MOCK is active the API still groups duplicate
    source_ids into excerpts — the template's iPhone-first list must work
    against the dashboard-mock harness too, not only the live FTS path."""
    from tools.dashboard import server

    rows = [
        {
            "id": "row-a",
            "source_id": "src-multi",
            "source_title": "Multi-hit mock source",
            "source_type": "session",
            "source_created_at": "2026-04-20T00:00:00Z",
            "project": "autonomy",
            "result_type": "thought",
            "turn_number": 12,
            "content": "first excerpt",
            "rank": -3.0,
        },
        {
            "id": "row-b",
            "source_id": "src-multi",
            "source_title": "Multi-hit mock source",
            "source_type": "session",
            "source_created_at": "2026-04-20T00:00:00Z",
            "project": "autonomy",
            "result_type": "thought",
            "turn_number": 47,
            "content": "second excerpt",
            "rank": -2.5,
        },
    ]

    os.environ["DASHBOARD_MOCK"] = "1"
    try:
        with TestClient(test_app) as client:
            with patch.object(
                server.dao_beads, "search",
                staticmethod(lambda q, limit=20, project=None,
                             order="relevance", session_type=None: rows),
                create=True,
            ):
                r = client.get("/api/search?q=mock&group=1")
                assert r.status_code == 200
                groups = r.json()
    finally:
        os.environ.pop("DASHBOARD_MOCK", None)

    assert len(groups) == 1
    g = groups[0]
    assert g["match_count"] == 2
    assert g["source_type"] == "session"
    assert sorted(e["turn_number"] for e in g["excerpts"]) == [12, 47]
