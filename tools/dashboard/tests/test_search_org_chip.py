"""Tests for the search bar org chip + peer-pill affordances (auto-13134).

Three contracts:

1. ``GET /api/orgs`` enumerates every org slug present under
   ``data/orgs/*.db`` — the dropdown reads this list to render its
   options.
2. ``GET /api/search?q=…&only_org=…`` forwards ``only_org`` straight
   through to :func:`tools.graph.ops.search` so the org chip can pin
   the result set to a single org.
3. The result-row enrichment attaches an ``is_peer`` boolean by
   comparing the row's resolved org slug against the caller-org bound
   by :class:`ApiIdentityMiddleware`. Same-org rows get ``is_peer=False``;
   cross-org rows get ``is_peer=True``. This is what the UI reads to
   paint the "peer" pill on cards.
"""

from __future__ import annotations

from unittest.mock import patch

from starlette.testclient import TestClient


def test_api_orgs_returns_known_slugs(shipped_settings_orgs, test_app):
    """``/api/orgs`` must surface every org under ``data/orgs/*.db`` so the
    org-chip dropdown can render an option per org."""
    with TestClient(test_app) as client:
        r = client.get("/api/orgs")
        assert r.status_code == 200
        body = r.json()

    assert "orgs" in body
    slugs = {entry["org"]["slug"] for entry in body["orgs"]}
    # ``shipped_settings_orgs`` bootstraps autonomy + anchore + personal.
    assert {"autonomy", "anchore", "personal"} <= slugs

    # Each entry must carry the identity fields the dropdown reads.
    for entry in body["orgs"]:
        ident = entry["identity_resolved"]
        assert set(ident) >= {"slug", "name", "color", "initial", "favicon"}


def test_search_with_only_org_pin(test_app):
    """``?only_org=anchore`` must reach :func:`ops.search` verbatim — that's
    how the org chip pins the result set to a single org's DB."""
    from tools.dashboard import server

    captured: dict = {}

    def fake_search(q, **kwargs):
        captured["q"] = q
        captured.update(kwargs)
        return []

    with patch.object(server.graph_ops, "search", side_effect=fake_search):
        with TestClient(test_app) as client:
            r = client.get("/api/search?q=x&only_org=anchore")
            assert r.status_code == 200

    assert captured["q"] == "x"
    assert captured.get("only_org") == "anchore"


def test_search_results_carry_is_peer_flag(test_app):
    """Rows whose resolved org slug differs from the caller-org get
    ``is_peer=True``; same-org rows get ``is_peer=False``. Caller-org is
    bound by ``ApiIdentityMiddleware`` from the ``X-Graph-Org`` header."""
    from tools.dashboard import server

    rows = [
        # Own-org row — caller is autonomy, row is autonomy.
        {
            "source_id": "src-own",
            "source_title": "Own org row",
            "source_type": "note",
            "result_type": "thought",
            "project": "autonomy",
            "platform": "local",
            "org": "autonomy",
            "rank": -9.0,
            "content": "x",
        },
        # Peer-org row — caller is autonomy, row is anchore.
        {
            "source_id": "src-peer",
            "source_title": "Peer org row",
            "source_type": "note",
            "result_type": "thought",
            "project": "anchore",
            "platform": "local",
            "org": "anchore",
            "rank": -8.0,
            "content": "y",
        },
    ]

    with patch.object(server.graph_ops, "search", return_value=rows):
        with TestClient(test_app) as client:
            r = client.get(
                "/api/search?q=x",
                headers={"X-Graph-Org": "autonomy"},
            )
            assert r.status_code == 200
            body = r.json()

    by_slug = {}
    for entry in body:
        org = entry.get("org")
        slug = org.get("slug") if isinstance(org, dict) else org
        by_slug[slug] = entry

    assert by_slug["autonomy"]["is_peer"] is False
    assert by_slug["anchore"]["is_peer"] is True


def test_search_results_no_peer_flag_when_caller_unknown(test_app):
    """When no caller-org is set (no header, no env), every row collapses to
    ``is_peer=False`` because "peer" only makes sense relative to a known
    seat."""
    from tools.dashboard import server

    rows = [
        {
            "source_id": "src-anchore",
            "source_type": "note",
            "result_type": "thought",
            "project": "anchore",
            "org": "anchore",
            "rank": -7.0,
            "content": "z",
        },
    ]

    with patch.object(server.graph_ops, "search", return_value=rows):
        # Ensure no GRAPH_ORG env leaks in from the host process.
        with patch.dict("os.environ", {}, clear=False) as _:
            import os as _os
            _os.environ.pop("GRAPH_ORG", None)
            with TestClient(test_app) as client:
                r = client.get("/api/search?q=z")
                assert r.status_code == 200
                body = r.json()

    assert body[0]["is_peer"] is False


def test_search_grouped_results_carry_is_peer_flag(test_app):
    """Grouped rows (?group=1) preserve the ``is_peer`` flag — the v3 search
    page fetches with ``group=1`` so the flag must survive the collapse."""
    from tools.dashboard import server

    rows = [
        {
            "source_id": "src-peer",
            "source_title": "Peer source",
            "source_type": "note",
            "result_type": "thought",
            "project": "anchore",
            "org": "anchore",
            "rank": -6.0,
            "turn_number": 1,
            "content": "first",
        },
        {
            "source_id": "src-peer",
            "source_title": "Peer source",
            "source_type": "note",
            "result_type": "thought",
            "project": "anchore",
            "org": "anchore",
            "rank": -5.0,
            "turn_number": 2,
            "content": "second",
        },
    ]

    with patch.object(server.graph_ops, "search", return_value=rows):
        with TestClient(test_app) as client:
            r = client.get(
                "/api/search?q=x&group=1",
                headers={"X-Graph-Org": "autonomy"},
            )
            assert r.status_code == 200
            body = r.json()

    assert len(body) == 1
    assert body[0]["is_peer"] is True
