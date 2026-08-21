"""The lease client sends the session bearer, so gated routes accept it.

The dashboard's Agent Test routes require an authenticated caller. An agent
session holds its token in CROSSTALK_TOKEN; if the client omits the bearer,
every gated call classifies COMPATIBILITY and 401s despite a valid token in
the environment — the exact client-half gap that took the fleet down twice on
2026-08-19 when a guard landed ahead of the client. This pins the client half.
"""

from __future__ import annotations

import pytest

from tools.agent_test import lease_client


def test_headers_carry_the_bearer_when_a_token_is_present(monkeypatch):
    monkeypatch.setenv("CROSSTALK_TOKEN", "tok-abc")
    headers = lease_client._headers()
    assert headers["Authorization"] == "Bearer tok-abc"
    assert headers["Content-Type"] == "application/json"


def test_headers_omit_the_bearer_when_no_token(monkeypatch):
    monkeypatch.delenv("CROSSTALK_TOKEN", raising=False)
    headers = lease_client._headers()
    assert "Authorization" not in headers


@pytest.mark.parametrize(
    "call",
    [
        lambda: lease_client.lease_request("status"),
        lambda: lease_client.telemetry_request("status"),
        lambda: lease_client.duration_request("estimate"),
    ],
)
def test_every_request_site_sends_the_bearer(monkeypatch, call):
    monkeypatch.setenv("CROSSTALK_TOKEN", "tok-xyz")
    seen = {}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"ok": true}'

    def _capture(request, timeout=None, context=None):
        seen["auth"] = request.headers.get("Authorization")
        return _Resp()

    monkeypatch.setattr(lease_client.urllib.request, "urlopen", _capture)
    call()
    assert seen["auth"] == "Bearer tok-xyz"
