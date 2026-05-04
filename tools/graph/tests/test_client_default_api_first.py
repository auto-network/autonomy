"""``get_client()`` defaults to HttpClient — auto-lq20j contract.

The dispatch flip codifies that HTTP-via-dashboard is the canonical write
path. Tests here pin the production default (``_FORCE_HOST_DIRECT = False``)
to override the conftest's host-direct test isolation, then verify each
branch of ``get_client()``:

  * no env, no flag                          → HttpClient @ default URL
  * ``GRAPH_API`` set, no flag               → HttpClient @ that URL
  * no env, ``_FORCE_HOST_DIRECT = True``    → ``ops`` module
  * ``GRAPH_API`` set + ``_FORCE_HOST_DIRECT = True`` → ``ops`` module wins

The flag is the single escape hatch; ``GRAPH_API`` is now a URL override,
not a feature flag.
"""

from __future__ import annotations

import pytest

from tools.graph import client as _client_mod
from tools.graph import ops
from tools.graph.client import HttpClient, get_client


@pytest.fixture
def api_default(monkeypatch):
    """Restore the production default: HttpClient is the result of get_client()."""
    monkeypatch.setattr(_client_mod, "_FORCE_HOST_DIRECT", False)


def test_get_client_returns_httpclient_by_default(api_default, monkeypatch):
    """No GRAPH_API, no --force-host → HttpClient against the default URL."""
    monkeypatch.delenv("GRAPH_API", raising=False)
    client = get_client()
    assert isinstance(client, HttpClient)
    # The default URL is the host dashboard, normalised (no trailing slash).
    assert client.base_url == "https://localhost:8080"


def test_get_client_returns_httpclient_with_graph_api_set(api_default, monkeypatch):
    """``GRAPH_API`` is a URL override, not a feature flag — still HttpClient."""
    monkeypatch.setenv("GRAPH_API", "https://other.example:9443")
    client = get_client()
    assert isinstance(client, HttpClient)
    assert client.base_url == "https://other.example:9443"


def test_get_client_returns_ops_when_force_host_set(monkeypatch):
    """``_FORCE_HOST_DIRECT = True`` → ``ops`` module (in-process writes)."""
    monkeypatch.setattr(_client_mod, "_FORCE_HOST_DIRECT", True)
    monkeypatch.delenv("GRAPH_API", raising=False)
    client = get_client()
    assert client is ops


def test_get_client_returns_ops_when_force_host_set_even_with_graph_api(monkeypatch):
    """The flag overrides ``GRAPH_API`` — ``--force-host`` is the escape hatch."""
    monkeypatch.setattr(_client_mod, "_FORCE_HOST_DIRECT", True)
    monkeypatch.setenv("GRAPH_API", "https://other.example:9443")
    client = get_client()
    assert client is ops
