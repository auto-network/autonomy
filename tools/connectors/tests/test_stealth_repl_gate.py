"""The REPL's HTTP surface refuses unauthenticated callers everywhere.

repl_auth's ladder is proven against real DBs in test_repl_auth.py; here
it is stubbed so these tests pin the HANDLER contract: every POST surface
authenticates before doing anything, and an unauthenticated /health leaks
no browsing state.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path

import pytest

from tools.connectors import repl_auth, stealth_repl

GOOD_TOKEN = "good-token"
CALLER = repl_auth.ReplCaller(
    session="auto-0810-000001", workspace_id="finance-ws", org="acme")


@pytest.fixture
def server(tmp_path, monkeypatch):
    def fake_authenticate(*, autonomy_root, authorization):
        assert autonomy_root == tmp_path  # the controller's root, verbatim
        if authorization == f"Bearer {GOOD_TOKEN}":
            return CALLER
        raise repl_auth.ReplAuthError("invalid or revoked token", status=401)

    monkeypatch.setattr(stealth_repl.repl_auth, "authenticate",
                        fake_authenticate)
    controller = stealth_repl.BrowserController(
        "eversource", tmp_path / "profile", tmp_path / "downloads",
        None, autonomy_root=tmp_path)  # never started: no browser needed
    handler = type("TestReplHandler", (stealth_repl.ReplHandler,),
                   {"controller": controller})
    httpd = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    thread.join(timeout=5)


def _call(base: str, path: str, *, method: str = "GET", body: bytes = b"",
          token: str | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(base + path, data=body or None,
                                     method=method)
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


@pytest.mark.parametrize("path,body", [
    ("/api/login", b'{"target_key": "connector.eversource.login"}'),
    ("/api/command", b'{"command": "status"}'),
    ("/", b"status"),
])
def test_every_post_surface_refuses_without_token(server, path, body):
    status, payload = _call(server, path, method="POST", body=body)
    assert status == 401
    assert payload == {"ok": False, "error": "invalid or revoked token"}


def test_authenticated_command_reaches_the_controller(server):
    status, payload = _call(server, "/api/command", method="POST",
                            body=b'{"command": "url"}', token=GOOD_TOKEN)
    # Past the gate: the controller answers (no browser page in this test).
    assert status == 400
    assert payload["error"] == "browser page is not ready"


def test_unauthenticated_health_is_liveness_only(server):
    status, payload = _call(server, "/health")
    assert status == 200
    assert payload == {
        "ok": True,
        "started_at": payload["started_at"],
        "page_ready": False,
        "authenticated": False,
    }
    for leaked in ("url", "title", "profile_dir", "download_dir", "pid",
                   "provider"):
        assert leaked not in payload


def test_authenticated_health_is_full_status(server):
    status, payload = _call(server, "/health", token=GOOD_TOKEN)
    assert status == 200
    assert payload["authenticated"] is True
    assert "profile_dir" in payload and "url" in payload


def test_login_uses_the_derived_workspace_not_a_request_field(server, monkeypatch):
    seen = {}

    def fake_secure_login(self, request, caller):
        seen["caller"] = caller
        return {"authenticated": True}

    monkeypatch.setattr(stealth_repl.BrowserController, "secure_login",
                        fake_secure_login)
    status, payload = _call(
        server, "/api/login", method="POST",
        body=b'{"target_key": "k", "workspace": "attacker-chosen"}',
        token=GOOD_TOKEN)
    assert status == 200 and payload["ok"] is True
    assert seen["caller"] == CALLER  # identity came from the token, not the body
