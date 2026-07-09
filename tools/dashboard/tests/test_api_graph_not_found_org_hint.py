"""Cross-org hint on graph-source 404s.

``graph tail <tmux>`` on a session homed in another org used to dead-end
in a bare "not found": the tmux→source_id resolution is org-agnostic but
the source read is scoped to ``X-Graph-Org``. The 404 bodies of
``/api/graph/source/{id}`` and ``/api/graph/{id}`` now carry an
``exists_in_org`` hint (existence only — no content) so the CLI can say
which org holds the ID and how to retry.
"""

from __future__ import annotations

from unittest.mock import patch

from starlette.testclient import TestClient


_MISSING_ID = "2b3a4030-9934-4a18-8931-a381e7d4f56d"

_LOCATE_HIT = {
    "org": "anchore",
    "id": _MISSING_ID,
    "type": "session",
}


def test_source_get_404_names_home_org(test_app):
    """Scoped miss + locate hit in another org → enriched 404 body."""
    from tools.dashboard import server

    with patch.object(server.graph_ops, "get_source", return_value=None):
        with patch.object(server.graph_ops, "locate_source_org",
                          return_value=dict(_LOCATE_HIT)):
            with TestClient(test_app) as client:
                r = client.get(f"/api/graph/source/{_MISSING_ID}",
                               headers={"X-Graph-Org": "autonomy"})

    assert r.status_code == 404
    body = r.json()
    assert body["error"] == "not found in org 'autonomy'"
    assert body["exists_in_org"] == "anchore"
    assert body["source_id"] == _MISSING_ID
    assert body["source_type"] == "session"


def test_source_get_404_stays_plain_when_id_exists_nowhere(test_app):
    from tools.dashboard import server

    with patch.object(server.graph_ops, "get_source", return_value=None):
        with patch.object(server.graph_ops, "locate_source_org",
                          return_value=None):
            with TestClient(test_app) as client:
                r = client.get(f"/api/graph/source/{_MISSING_ID}",
                               headers={"X-Graph-Org": "autonomy"})

    assert r.status_code == 404
    body = r.json()
    assert body == {"error": "not found"}


def test_source_get_404_no_hint_when_hit_is_caller_org(test_app):
    """Defensive: a locate hit in the caller's own org adds no hint —
    the miss then means something else (e.g. a race), not a scope gap."""
    from tools.dashboard import server

    with patch.object(server.graph_ops, "get_source", return_value=None):
        with patch.object(server.graph_ops, "locate_source_org",
                          return_value={**_LOCATE_HIT, "org": "autonomy"}):
            with TestClient(test_app) as client:
                r = client.get(f"/api/graph/source/{_MISSING_ID}",
                               headers={"X-Graph-Org": "autonomy"})

    assert r.status_code == 404
    assert r.json() == {"error": "not found"}


def test_graph_resolve_404_names_home_org(test_app):
    """The universal resolver's final 404 carries the same hint."""
    from tools.dashboard import server

    with patch.object(server.graph_ops, "get_source", return_value=None):
        with patch.object(server.graph_ops, "get_attachment",
                          return_value=None):
            with patch.object(server.graph_ops, "get_comment",
                              return_value=None):
                with patch.object(server.graph_ops, "locate_source_org",
                                  return_value=dict(_LOCATE_HIT)):
                    with TestClient(test_app) as client:
                        r = client.get(f"/api/graph/{_MISSING_ID}",
                                       headers={"X-Graph-Org": "autonomy"})

    assert r.status_code == 404
    body = r.json()
    assert body["exists_in_org"] == "anchore"
    assert body["source_id"] == _MISSING_ID
