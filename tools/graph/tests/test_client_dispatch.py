"""Dispatcher tests for ``tools.graph.client.get_client``.

Post auto-lq20j: ``get_client()`` defaults to :class:`HttpClient`. The
``_FORCE_HOST_DIRECT`` module switch (flipped by the CLI's
``--force-host`` flag) is the only way back to direct ``ops.*`` writes.
``GRAPH_API`` is now a base-URL override, not a feature flag.

The conftest's ``_isolate_graph_env`` autouse fixture pins
``_FORCE_HOST_DIRECT = True`` for every graph test. These tests flip it
back to ``False`` to exercise the production default.
"""

from __future__ import annotations

import pytest

from tools.graph import client as _client_mod
from tools.graph import ops
from tools.graph.client import HttpClient, get_client


@pytest.fixture
def api_first(monkeypatch):
    """Restore the production default (HttpClient) for these dispatch tests."""
    monkeypatch.setattr(_client_mod, "_FORCE_HOST_DIRECT", False)


def test_default_returns_httpclient(api_first, monkeypatch):
    """No GRAPH_API, no --force-host → HttpClient against localhost dashboard."""
    monkeypatch.delenv("GRAPH_API", raising=False)
    client = get_client()
    assert isinstance(client, HttpClient)
    assert client.base_url == "https://localhost:8080"


def test_http_client_when_graph_api_set(api_first, monkeypatch):
    """``GRAPH_API`` overrides the base URL — still HttpClient."""
    monkeypatch.setenv("GRAPH_API", "https://localhost:8080")
    client = get_client()
    assert isinstance(client, HttpClient)
    assert client.base_url == "https://localhost:8080"


def test_http_client_strips_trailing_slash(api_first, monkeypatch):
    """Base URL is normalised — no double slashes when paths are appended."""
    monkeypatch.setenv("GRAPH_API", "https://localhost:8080/")
    client = get_client()
    assert isinstance(client, HttpClient)
    assert client.base_url == "https://localhost:8080"


def test_force_host_returns_ops_module(monkeypatch):
    """``--force-host`` (i.e. ``_FORCE_HOST_DIRECT = True``) → ops module."""
    monkeypatch.setattr(_client_mod, "_FORCE_HOST_DIRECT", True)
    monkeypatch.delenv("GRAPH_API", raising=False)
    client = get_client()
    assert client is ops


def test_host_path_delegates_to_ops(monkeypatch, tmp_path):
    """``--force-host`` routes ``search`` straight through ``ops.search``."""
    monkeypatch.setattr(_client_mod, "_FORCE_HOST_DIRECT", True)
    monkeypatch.delenv("GRAPH_API", raising=False)
    monkeypatch.setenv("GRAPH_DB", str(tmp_path / "graph.db"))
    # Empty DB returns empty results; the point here is no exception, no http.
    results = get_client().search("nothing here")
    assert results == []
