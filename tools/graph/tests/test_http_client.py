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


def test_request_vault_open_derives_session_server_side_and_returns_receipt():
    client = _make_client()
    captured = []

    def fake_urlopen(req, timeout=None, context=None):
        body = json.loads(req.data) if req.data else None
        captured.append((req.get_method(), req.full_url, body, timeout))
        if req.get_method() == "POST":
            return _FakeResponse({"id": "open-1"})
        return _FakeResponse({
            "result": {
                "approved": True,
                "execution": {
                    "ok": True,
                    "receipt": {
                        "release_id": "open-1",
                        "delivery": "session-ramfs",
                        "path": "/run/secrets/mac.ssh",
                        "ttl_seconds": 60,
                    },
                },
            },
        })

    with patch("urllib.request.urlopen", fake_urlopen):
        receipt = client.request_vault_open(
            "autonomy.vault.secured", "mac.ssh", org="autonomy",
        )

    assert receipt == {
        "release_id": "open-1",
        "delivery": "session-ramfs",
        "path": "/run/secrets/mac.ssh",
        "ttl_seconds": 60,
    }
    assert captured[0][2] == {
        "kind": "vault_open",
        "request": {
            "set_id": "autonomy.vault.secured",
            "key": "mac.ssh",
            "ttl_seconds": 60,
        },
    }
    assert "session" not in captured[0][2]
    assert "/api/approvals/open-1?wait=" in captured[1][1]


def test_personal_seal_uses_narrow_endpoint_with_policy_but_no_opener_material():
    client = _make_client()
    captured = {}

    def fake_urlopen(req, timeout=None, context=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        return _FakeResponse(
            {"id": "setting-1", "key": "autonomy:mac.ssh"}, status=201,
        )

    with patch("urllib.request.urlopen", fake_urlopen):
        assert client.seal_personal_setting(
            "mac.ssh", "fake-private-key", policy_class_id="class-1",
        ) == "setting-1"

    assert captured["url"].endswith("/api/identity/vault-settings")
    assert captured["body"] == {
        "key": "mac.ssh",
        "value": "fake-private-key",
        "policy_class_id": "class-1",
    }
    assert "openers" not in captured["body"]
    assert client.last_write_report == {
        "id": "setting-1", "key": "autonomy:mac.ssh",
    }


def test_search_passes_or_mode_and_tag():
    """Optional params (or, tag) flow through to query string."""
    client = _make_client()
    captured = {}

    def fake_urlopen(req, timeout=None, context=None):
        captured["url"] = req.full_url
        return _FakeResponse([])

    with patch("urllib.request.urlopen", fake_urlopen):
        client.search("q", or_mode=True, tag="pitfall")

    assert "or=1" in captured["url"]
    assert "tag=pitfall" in captured["url"]


def test_search_passes_nondefault_ranker():
    client = _make_client()
    captured = {}

    def fake_urlopen(req, timeout=None, context=None):
        captured["url"] = req.full_url
        return _FakeResponse([])

    with patch("urllib.request.urlopen", fake_urlopen):
        client.search("q", ranker="smart")

    assert "ranker=smart" in captured["url"]


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


def test_locate_source_org_reads_enriched_404_body():
    """A cross-org miss (404 body carrying ``exists_in_org``) surfaces as
    a locate hit so the CLI can name the source's home org."""
    client = _make_client()
    import urllib.error

    body = {
        "error": "not found in org 'autonomy'",
        "exists_in_org": "anchore",
        "source_id": "2b3a4030-9934-4a18-8931-a381e7d4f56d",
        "source_type": "session",
    }

    def fake_urlopen(req, timeout=None, context=None):
        raise urllib.error.HTTPError(
            req.full_url, 404, "not found", {},
            io.BytesIO(json.dumps(body).encode()),
        )

    with patch("urllib.request.urlopen", fake_urlopen):
        hit = client.locate_source_org("2b3a4030")
    assert hit == {
        "org": "anchore",
        "id": "2b3a4030-9934-4a18-8931-a381e7d4f56d",
        "type": "session",
    }


def test_locate_source_org_plain_404_returns_none():
    """A miss with no org hint (ID exists nowhere) stays None."""
    client = _make_client()
    import urllib.error

    def fake_urlopen(req, timeout=None, context=None):
        raise urllib.error.HTTPError(
            req.full_url, 404, "not found", {},
            io.BytesIO(json.dumps({"error": "not found"}).encode()),
        )

    with patch("urllib.request.urlopen", fake_urlopen):
        assert client.locate_source_org("missing") is None


def test_locate_source_org_in_scope_hit_reports_source_org():
    """When the source resolves normally, the hit echoes its org field."""
    client = _make_client()

    def fake_urlopen(req, timeout=None, context=None):
        return _FakeResponse({"id": "abc-full", "type": "note", "org": "autonomy"})

    with patch("urllib.request.urlopen", fake_urlopen):
        hit = client.locate_source_org("abc")
    assert hit == {"org": "autonomy", "id": "abc-full", "type": "note"}


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


def test_list_session_status_calls_dashboard_endpoint():
    """Session status routes to the dashboard DAO endpoint with optional since."""
    client = _make_client()
    captured: dict = {}

    def fake_urlopen(req, timeout=None, context=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        return _FakeResponse([{"tmux_name": "auto-test", "is_live": 1}])

    with patch("urllib.request.urlopen", fake_urlopen):
        rows = client.list_session_status(since="24h")

    assert "/api/dao/session_status" in captured["url"]
    assert "since=24h" in captured["url"]
    assert captured["method"] == "GET"
    assert rows == [{"tmux_name": "auto-test", "is_live": 1}]


def test_read_source_full_does_not_send_max_chars():
    """``HttpClient.read_source_full`` must not put ``max_chars`` on the
    wire. The page-load route (``GET /api/graph/{id}``) is unbounded by
    design — pinning this contract keeps a future refactor from quietly
    re-adding a query param the server doesn't read anyway.
    """
    client = _make_client()
    captured: dict = {}

    def fake_urlopen(req, timeout=None, context=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        return _FakeResponse(
            {"source": {"id": "abc"}, "entries": [], "truncated": False,
             "total_chars": 0}
        )

    with patch("urllib.request.urlopen", fake_urlopen):
        client.read_source_full("abc-123")

    assert "/api/graph/abc-123" in captured["url"]
    assert "max_chars" not in captured["url"], (
        f"HttpClient.read_source_full must not transmit max_chars; "
        f"got URL: {captured['url']!r}"
    )
    assert captured["method"] == "GET"


def test_read_source_full_signature_omits_max_chars():
    """Compile-time check: ``max_chars`` is not a parameter of
    ``HttpClient.read_source_full``. The HTTP route is unbounded by
    design; the lying parameter has been deleted.
    """
    import inspect
    sig = inspect.signature(HttpClient.read_source_full)
    assert "max_chars" not in sig.parameters, (
        f"HttpClient.read_source_full.max_chars should have been removed; "
        f"got signature: {sig}"
    )


def test_read_source_full_forwards_window_and_tail():
    """Slice params still flow to the wire — only ``max_chars`` was deleted."""
    client = _make_client()
    captured: dict = {}

    def fake_urlopen(req, timeout=None, context=None):
        captured["url"] = req.full_url
        return _FakeResponse({"source": {"id": "abc"}, "entries": []})

    # tail mode → ``?from=-N``
    with patch("urllib.request.urlopen", fake_urlopen):
        client.read_source_full("abc-123", tail_n=5)
    assert "from=-5" in captured["url"]

    # around-turn mode → ``?turn=N&window=W``
    with patch("urllib.request.urlopen", fake_urlopen):
        client.read_source_full("abc-123", around_turn=10, window=3)
    assert "turn=10" in captured["url"]
    assert "window=3" in captured["url"]


def test_ingest_docs_posts_to_api_graph_docs():
    """HttpClient.ingest_docs → POST /api/graph/docs with path/org/force body
    and the ``X-Graph-Org`` header (so the host writes to the right RW DB)."""
    client = _make_client()
    captured: dict = {}

    def fake_urlopen(req, timeout=None, context=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["body"] = json.loads(req.data.decode())
        captured["org_header"] = req.get_header("X-graph-org")
        return _FakeResponse({"ok": True, "output": "Total: 3 ingested, 0 skipped"})

    with patch("urllib.request.urlopen", fake_urlopen):
        result = client.ingest_docs("/workspace/repo/docs", org="blindhash", force=True)

    assert "/api/graph/docs" in captured["url"]
    assert captured["method"] == "POST"
    assert captured["body"] == {
        "path": "/workspace/repo/docs",
        "org": "blindhash",
        "force": True,
    }
    assert captured["org_header"] == "blindhash"
    assert result == {"ok": True, "output": "Total: 3 ingested, 0 skipped"}


def test_ingest_docs_omits_force_and_org_when_absent():
    """Without org/force, the body carries only ``path`` — no falsey noise."""
    client = _make_client()
    captured: dict = {}

    def fake_urlopen(req, timeout=None, context=None):
        captured["body"] = json.loads(req.data.decode())
        return _FakeResponse({"ok": True, "output": "Total: 1 ingested, 0 skipped"})

    with patch("urllib.request.urlopen", fake_urlopen):
        client.ingest_docs("/tmp/TOOL.md")

    assert captured["body"] == {"path": "/tmp/TOOL.md"}


def test_get_dispatch_wait_status_calls_dashboard_endpoint():
    """Dispatch wait status routes to the dedicated dashboard endpoint."""
    client = _make_client()
    captured: dict = {}

    def fake_urlopen(req, timeout=None, context=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        return _FakeResponse({"state": "waiting", "bead_id": "auto-test"})

    with patch("urllib.request.urlopen", fake_urlopen):
        status = client.get_dispatch_wait_status("auto-test")

    assert "/api/dispatch/wait/auto-test" in captured["url"]
    assert captured["method"] == "GET"
    assert status == {"state": "waiting", "bead_id": "auto-test"}


# ── auto-w1ktf: the container session token rides as an additive bearer ──


def test_headers_add_bearer_additively_when_crosstalk_token_set(monkeypatch):
    """The container ``CROSSTALK_TOKEN`` is sent as ``Authorization: Bearer``,
    ADDITIVELY alongside ``X-Graph-Org``. Additive means no server behaviour
    changes until the h4kzx flip — there is no window where a token-requiring
    server refuses a caller that has not yet sent a bearer."""
    monkeypatch.setenv("CROSSTALK_TOKEN", "sess-tok-123")
    h = _make_client()._headers(org="anchore")
    assert h["Authorization"] == "Bearer sess-tok-123"
    assert h["X-Graph-Org"] == "anchore"  # unchanged — the bearer rides alongside


def test_headers_omit_bearer_when_no_crosstalk_token(monkeypatch):
    """A host caller has no ``CROSSTALK_TOKEN`` and sends no bearer — a local,
    org-less caller server-side."""
    monkeypatch.delenv("CROSSTALK_TOKEN", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    h = _make_client()._headers()
    assert "Authorization" not in h


# ── the session bearer reaches the SETTINGS path ─────────────
#
# auto-w1ktf's title is "the graph CLI sends the session-token bearer ON
# SETTINGS REQUESTS". It landed the bearer in `HttpClient._headers` and
# asserted on `_headers` — but every Settings call builds its headers with
# the module-level `_settings_headers`, which sent `X-Graph-Org` and nothing
# else. So the suite was green while the named path carried no credential,
# and an authentication guard on the Settings readers took the fleet down
# twice before anyone looked at which builder was in play.
#
# These tests therefore call the SETTINGS METHODS the way a caller reaches
# them and inspect the request that actually goes out. A test that never
# calls a settings method cannot say anything about settings requests,
# however green it is.


def _capture_settings_request(monkeypatch, token: str | None):
    """Drive a real settings read and return the outgoing request."""
    if token is None:
        monkeypatch.delenv("CROSSTALK_TOKEN", raising=False)
    else:
        monkeypatch.setenv("CROSSTALK_TOKEN", token)
    captured: dict = {}

    def fake_urlopen(req, timeout=None, context=None):
        captured["headers"] = dict(req.headers)
        captured["url"] = req.full_url
        return _FakeResponse({"members": []})

    with patch("urllib.request.urlopen", fake_urlopen):
        _make_client().read_set("autonomy.workspace.mount", org="anchore")
    return captured


def test_settings_read_sends_the_session_bearer(monkeypatch):
    captured = _capture_settings_request(monkeypatch, "sess-tok-settings")

    # urllib title-cases header names on the Request object.
    auth = captured["headers"].get("Authorization")
    assert auth == "Bearer sess-tok-settings", (
        f"a settings read went out with {auth!r}; this is the exact path the "
        f"bead names, and a guard requiring a credential 401s the whole fleet "
        f"without it"
    )
    assert "/api/graph/settings/" in captured["url"]


def test_settings_read_still_sends_the_org_header(monkeypatch):
    """The bearer is additive; it does not displace the scope selector."""
    captured = _capture_settings_request(monkeypatch, "sess-tok-settings")

    assert captured["headers"].get("X-graph-org") == "anchore"


def test_settings_read_omits_the_bearer_without_a_token(monkeypatch):
    """A host caller has none and must send none."""
    captured = _capture_settings_request(monkeypatch, None)

    assert "Authorization" not in captured["headers"]


def test_no_settings_call_site_can_opt_out_of_the_bearer():
    """It is attached per REQUEST, not per header builder.

    Attaching it per builder is what allowed one of two builders to be
    missed. This asserts the credential is applied at the single chokepoint
    every call passes through, so a third builder cannot reintroduce the gap.
    """
    import inspect

    from tools.graph import client as client_mod

    source = inspect.getsource(client_mod.HttpClient._request)
    assert "CROSSTALK_TOKEN" in source and "Authorization" in source, (
        "the bearer is no longer attached in _request; if it moved back into "
        "the header builders, a settings call can silently lose it again"
    )
