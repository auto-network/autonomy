"""Dispatcher tests for ``tools.graph.client.get_client``.

The single-place "am I in a container?" branch must select HttpClient when
``GRAPH_API`` is set, LocalClient otherwise. These tests pin those two
states and check the returned class.
"""

from __future__ import annotations

from tools.graph.client import GraphClient, HttpClient, LocalClient, get_client


def test_local_client_when_graph_api_unset(monkeypatch):
    """No GRAPH_API → LocalClient (host path)."""
    monkeypatch.delenv("GRAPH_API", raising=False)
    client = get_client()
    assert isinstance(client, LocalClient)
    assert isinstance(client, GraphClient)


def test_http_client_when_graph_api_set(monkeypatch):
    """GRAPH_API set → HttpClient (container path)."""
    monkeypatch.setenv("GRAPH_API", "https://localhost:8080")
    client = get_client()
    assert isinstance(client, HttpClient)
    assert isinstance(client, GraphClient)
    assert client.base_url == "https://localhost:8080"


def test_http_client_strips_trailing_slash(monkeypatch):
    """Base URL is normalised — no double slashes when paths are appended."""
    monkeypatch.setenv("GRAPH_API", "https://localhost:8080/")
    client = get_client()
    assert isinstance(client, HttpClient)
    assert client.base_url == "https://localhost:8080"


def test_local_client_delegates_to_ops(monkeypatch, tmp_path):
    """LocalClient.search calls into ops.search (which calls GraphDB)."""
    monkeypatch.delenv("GRAPH_API", raising=False)
    monkeypatch.setenv("GRAPH_DB", str(tmp_path / "graph.db"))
    # Empty DB returns empty results; the point here is no exception, no http.
    results = get_client().search("nothing here")
    assert results == []
