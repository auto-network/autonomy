"""Dispatcher tests for ``tools.graph.client.get_client``.

The single-place "am I in a container?" branch must select HttpClient when
``GRAPH_API`` is set; otherwise it returns the ``ops`` module itself,
which duck-types as a client (every HttpClient method has a matching
``ops.X`` top-level function with the same signature).
"""

from __future__ import annotations

from tools.graph import ops
from tools.graph.client import HttpClient, get_client


def test_ops_module_when_graph_api_unset(monkeypatch):
    """No GRAPH_API → host path returns the ops module itself."""
    monkeypatch.delenv("GRAPH_API", raising=False)
    client = get_client()
    assert client is ops


def test_http_client_when_graph_api_set(monkeypatch):
    """GRAPH_API set → HttpClient (container path)."""
    monkeypatch.setenv("GRAPH_API", "https://localhost:8080")
    client = get_client()
    assert isinstance(client, HttpClient)
    assert client.base_url == "https://localhost:8080"


def test_http_client_strips_trailing_slash(monkeypatch):
    """Base URL is normalised — no double slashes when paths are appended."""
    monkeypatch.setenv("GRAPH_API", "https://localhost:8080/")
    client = get_client()
    assert isinstance(client, HttpClient)
    assert client.base_url == "https://localhost:8080"


def test_host_path_delegates_to_ops(monkeypatch, tmp_path):
    """Host path routes ``search`` straight through ``ops.search``."""
    monkeypatch.delenv("GRAPH_API", raising=False)
    monkeypatch.setenv("GRAPH_DB", str(tmp_path / "graph.db"))
    # Empty DB returns empty results; the point here is no exception, no http.
    results = get_client().search("nothing here")
    assert results == []
