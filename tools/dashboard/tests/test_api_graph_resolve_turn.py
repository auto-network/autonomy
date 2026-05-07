"""Tests for ``/api/graph/{id}`` query-string passthrough of
``?turn=N&window=W`` to ``ops.read_source_full`` (auto-d1dvr).

The search results page (auto-bcxdr) ships per-turn deep links of the form
``/graph/{id}?turn=N``. Today the JSON endpoint backing the source viewer
ignores ``turn``/``window`` and falls back to the front-of-source ``max_chars``
slice, so deep links into long sessions render an empty list. These tests
pin the desired behaviour:

* ``turn`` + ``window`` query params reach ``read_source_full`` as
  ``around_turn`` / ``window`` kwargs.
* Without ``turn``, the existing call shape is preserved (no
  ``around_turn``).
* A non-integer ``turn`` returns 400, not silently ignoring the param.
"""

from __future__ import annotations

from unittest.mock import patch

from starlette.testclient import TestClient


_RESOLVED_SOURCE = {
    "id": "11111111-1111-1111-1111-111111111111",
    "title": "long session",
    "type": "session",
    "project": "autonomy",
    "created_at": "2026-04-01T10:00:00Z",
    "metadata": "{}",
}


def test_api_graph_resolve_passes_turn_through(test_app):
    """``GET /api/graph/{id}?turn=42&window=3`` invokes ``read_source_full``
    with ``around_turn=42, window=3``."""
    from tools.dashboard import server

    captured: dict = {}

    def fake_read_source_full(source_id, **kwargs):
        captured["source_id"] = source_id
        captured["kwargs"] = kwargs
        return {
            "source": _RESOLVED_SOURCE,
            "entries": [{"turn_number": 42, "role": "user",
                         "content": "x", "created_at": ""}],
            "truncated": False,
            "total_chars": 1,
            "comments": [],
        }

    with patch.object(server.graph_ops, "get_source",
                      return_value=_RESOLVED_SOURCE):
        with patch.object(server.graph_ops, "read_source_full",
                          side_effect=fake_read_source_full):
            with TestClient(test_app) as client:
                r = client.get(f"/api/graph/{_RESOLVED_SOURCE['id']}"
                               "?turn=42&window=3")
                assert r.status_code == 200, r.text

    assert captured["source_id"] == _RESOLVED_SOURCE["id"]
    assert captured["kwargs"].get("around_turn") == 42
    assert captured["kwargs"].get("window") == 3


def test_api_graph_resolve_no_turn_default_behaviour(test_app):
    """Without ``turn``, ``around_turn`` is None — the legacy
    front-of-source ``max_chars`` slice path runs unchanged."""
    from tools.dashboard import server

    captured: dict = {}

    def fake_read_source_full(source_id, **kwargs):
        captured["source_id"] = source_id
        captured["kwargs"] = kwargs
        return {
            "source": _RESOLVED_SOURCE,
            "entries": [],
            "truncated": False,
            "total_chars": 0,
            "comments": [],
        }

    with patch.object(server.graph_ops, "get_source",
                      return_value=_RESOLVED_SOURCE):
        with patch.object(server.graph_ops, "read_source_full",
                          side_effect=fake_read_source_full):
            with TestClient(test_app) as client:
                r = client.get(f"/api/graph/{_RESOLVED_SOURCE['id']}")
                assert r.status_code == 200, r.text

    assert captured["kwargs"].get("around_turn") is None


def test_api_graph_resolve_invalid_turn_400(test_app):
    """A non-integer ``turn`` value is rejected with 400 — silently
    dropping the param would land the user back on the broken
    front-of-source view."""
    from tools.dashboard import server

    with patch.object(server.graph_ops, "get_source",
                      return_value=_RESOLVED_SOURCE):
        with TestClient(test_app) as client:
            r = client.get(f"/api/graph/{_RESOLVED_SOURCE['id']}?turn=abc")
            assert r.status_code == 400


def test_api_graph_resolve_passes_tail_through(test_app):
    """``GET /api/graph/{id}?from=-7`` invokes ``read_source_full`` with
    ``tail_n=7`` so the dashboard / CLI / agents can read the trailing
    slice of a session in one round trip — no metadata-string parsing
    on the client side."""
    from tools.dashboard import server

    captured: dict = {}

    def fake_read_source_full(source_id, **kwargs):
        captured["source_id"] = source_id
        captured["kwargs"] = kwargs
        return {
            "source": _RESOLVED_SOURCE,
            "entries": [{"turn_number": 100, "role": "user",
                         "content": "x", "created_at": ""}],
            "truncated": False,
            "total_chars": 1,
            "comments": [],
        }

    with patch.object(server.graph_ops, "get_source",
                      return_value=_RESOLVED_SOURCE):
        with patch.object(server.graph_ops, "read_source_full",
                          side_effect=fake_read_source_full):
            with TestClient(test_app) as client:
                r = client.get(f"/api/graph/{_RESOLVED_SOURCE['id']}"
                               "?from=-7")
                assert r.status_code == 200, r.text

    assert captured["source_id"] == _RESOLVED_SOURCE["id"]
    assert captured["kwargs"].get("tail_n") == 7
    assert captured["kwargs"].get("around_turn") is None


def test_api_graph_resolve_invalid_from_400(test_app):
    """A non-integer ``from`` is rejected with 400, same as ``turn``."""
    from tools.dashboard import server

    with patch.object(server.graph_ops, "get_source",
                      return_value=_RESOLVED_SOURCE):
        with TestClient(test_app) as client:
            r = client.get(f"/api/graph/{_RESOLVED_SOURCE['id']}?from=abc")
            assert r.status_code == 400


def test_api_graph_resolve_positive_from_400(test_app):
    """Positive ``from`` is reserved for a future forward-range mode and
    must be rejected — silently treating it as no-op would surface the
    front-of-source slice and confuse callers expecting a tail."""
    from tools.dashboard import server

    with patch.object(server.graph_ops, "get_source",
                      return_value=_RESOLVED_SOURCE):
        with TestClient(test_app) as client:
            r = client.get(f"/api/graph/{_RESOLVED_SOURCE['id']}?from=5")
            assert r.status_code == 400
            r2 = client.get(f"/api/graph/{_RESOLVED_SOURCE['id']}?from=0")
            assert r2.status_code == 400


# ── Page-load is unbounded by design (auto-urf1s) ─────────────────────


def test_api_graph_resolve_passes_max_chars_zero(test_app):
    """``api_graph_resolve`` always calls ``read_source_full`` with
    ``max_chars=0`` (unbounded). Pre-fix it hard-coded ``max_chars=50000``,
    silently truncating long sessions and corrupting the source-viewer
    header metadata strip. The route now hands the full transcript to
    the browser surface."""
    from tools.dashboard import server

    captured: dict = {}

    def fake_read_source_full(source_id, **kwargs):
        captured["kwargs"] = kwargs
        return {
            "source": _RESOLVED_SOURCE,
            "entries": [],
            "truncated": False,
            "total_chars": 0,
            "comments": [],
        }

    with patch.object(server.graph_ops, "get_source",
                      return_value=_RESOLVED_SOURCE):
        with patch.object(server.graph_ops, "read_source_full",
                          side_effect=fake_read_source_full):
            with TestClient(test_app) as client:
                r = client.get(f"/api/graph/{_RESOLVED_SOURCE['id']}")
                assert r.status_code == 200, r.text

    assert captured["kwargs"].get("max_chars") == 0, (
        f"api_graph_resolve must pass max_chars=0 (unbounded); got "
        f"{captured['kwargs'].get('max_chars')!r}"
    )


def test_api_graph_resolve_ignores_query_max_chars(test_app):
    """``GET /api/graph/{id}?max_chars=10000`` does **not** override the
    server-side cap. The query param is dead end-to-end on this route —
    page-load is unbounded by design (no caller-side override). Pre-fix
    behaviour silently dropped the param too, but for the wrong reason
    (the value never reached read_source_full anyway). Post-fix this is
    explicit: the route hard-codes ``max_chars=0`` regardless."""
    from tools.dashboard import server

    captured: dict = {}

    def fake_read_source_full(source_id, **kwargs):
        captured["kwargs"] = kwargs
        return {
            "source": _RESOLVED_SOURCE,
            "entries": [],
            "truncated": False,
            "total_chars": 0,
            "comments": [],
        }

    with patch.object(server.graph_ops, "get_source",
                      return_value=_RESOLVED_SOURCE):
        with patch.object(server.graph_ops, "read_source_full",
                          side_effect=fake_read_source_full):
            with TestClient(test_app) as client:
                r = client.get(
                    f"/api/graph/{_RESOLVED_SOURCE['id']}?max_chars=10000"
                )
                assert r.status_code == 200, r.text

    # ``max_chars=10000`` query param is ignored — server hard-codes 0.
    assert captured["kwargs"].get("max_chars") == 0, (
        f"?max_chars= must not flow through; got "
        f"{captured['kwargs'].get('max_chars')!r}"
    )
