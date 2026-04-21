"""HttpClient tests — verify it talks to the dashboard endpoints correctly.

These tests intercept ``urllib.request.urlopen`` so the assertions cover
URL construction, query-string encoding, and JSON parsing without spinning
up a dashboard server.
"""

from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest

from tools.graph.client import GraphHttpError, HttpClient


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._body = json.dumps(payload).encode()
        self.status = status

    def read(self):
        return self._body


def _make_client():
    return HttpClient("https://localhost:8080")


def test_search_calls_api_graph_search_with_params():
    """HttpClient.search → GET /api/graph/search?q=...&limit=..."""
    client = _make_client()
    captured: dict = {}

    def fake_urlopen(req, timeout=None, context=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        return _FakeResponse([{"id": "abc", "content": "hit"}])

    with patch("urllib.request.urlopen", fake_urlopen):
        results = client.search("dispatch lifecycle", limit=10)

    assert "/api/graph/search" in captured["url"]
    assert "q=dispatch+lifecycle" in captured["url"]
    assert "limit=10" in captured["url"]
    assert captured["method"] == "GET"
    assert results == [{"id": "abc", "content": "hit"}]


def test_search_passes_project_and_or_mode():
    """Optional params (project, or, tag) flow through to query string."""
    client = _make_client()
    captured = {}

    def fake_urlopen(req, timeout=None, context=None):
        captured["url"] = req.full_url
        return _FakeResponse([])

    with patch("urllib.request.urlopen", fake_urlopen):
        client.search("q", project="autonomy", or_mode=True, tag="pitfall")

    assert "project=autonomy" in captured["url"]
    assert "or=1" in captured["url"]
    assert "tag=pitfall" in captured["url"]


def test_get_source_returns_dict_on_200():
    """200 OK with dict body is returned directly."""
    client = _make_client()
    payload = {"id": "abc-123", "title": "hello"}

    def fake_urlopen(req, timeout=None, context=None):
        return _FakeResponse(payload)

    with patch("urllib.request.urlopen", fake_urlopen):
        got = client.get_source("abc-123")
    assert got == payload


def test_get_source_returns_none_on_404():
    """404 maps to None — typed absence, no exception."""
    client = _make_client()
    import urllib.error

    def fake_urlopen(req, timeout=None, context=None):
        raise urllib.error.HTTPError(
            req.full_url, 404, "not found", {},
            io.BytesIO(json.dumps({"error": "not found"}).encode()),
        )

    with patch("urllib.request.urlopen", fake_urlopen):
        assert client.get_source("missing") is None


def test_http_error_other_than_404_raises():
    """5xx etc. raise GraphHttpError with status."""
    client = _make_client()
    import urllib.error

    def fake_urlopen(req, timeout=None, context=None):
        raise urllib.error.HTTPError(
            req.full_url, 500, "boom", {},
            io.BytesIO(json.dumps({"error": "internal"}).encode()),
        )

    with patch("urllib.request.urlopen", fake_urlopen):
        with pytest.raises(GraphHttpError) as exc_info:
            client.get_source("any")
    assert exc_info.value.status == 500


def test_list_sources_unwraps_envelope():
    """``{"sources": [...]}`` envelope is unwrapped to a list."""
    client = _make_client()

    def fake_urlopen(req, timeout=None, context=None):
        return _FakeResponse({"sources": [{"id": "1"}, {"id": "2"}]})

    with patch("urllib.request.urlopen", fake_urlopen):
        rows = client.list_sources(limit=10)
    assert [r["id"] for r in rows] == ["1", "2"]


def test_list_attachments_requires_source_id():
    """Calling without source_id raises NotImplementedError (no scan endpoint)."""
    client = _make_client()
    with pytest.raises(NotImplementedError):
        client.list_attachments()
