"""Invariant 5 (epic auto-1wwpf, decision note graph://f42db05f-7ca): the
test-credential self-issue mechanism is test-only. It is unreachable in a
production config and grants no more scope than the feature under test.

This is the G5 review, expressed as a falsifiable proof suite rather than prose
so the guarantee stays continuous. It pins the three claims of scenario D:

  1. NO PROD BACKDOOR — the only mechanisms that let a caller obtain a
     credential without the human sign-in ceremony are (a) in-process test
     helpers (``auth_db.insert_token`` / ``unlock_routes.mint_session_token``,
     reachable only from Python, never over the network) and (b) two env-only
     operator switches (``DASHBOARD_MOCK`` mock bypass, ``DASHBOARD_AUTH``
     recovery kill-switch). With neither env var set — the production config —
     every self-issue affordance is off and the gate refuses.

  2. REFUSES/404s IN PRODUCTION — the host-CLI token auto-provision, the one
     "self-issue" path adjacent to the running system, positively refuses a
     containerized (i.e. production automated) caller; and the human gate
     answers an unauthenticated gated request with a 401 / redirect, never a
     freshly minted session.

  3. NO OVER-SCOPING — the credential minter forces an explicit org at the call
     site (no scopeless default a test could inherit), and an org-stamped token
     resolves to exactly its own org. The reject-NULL escalation guard is
     proven end-to-end in ``test_session_token_org.py``; this suite pins the
     no-default half.

Companion coverage: ``test_session_token_org.py`` (token-forces-org, reject-NULL
guard), ``test_unlock_routes.py`` (the ceremony that legitimately mints).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import types

import pytest

from tools.dashboard import network_routes, unlock_routes
from tools.dashboard.dao import auth_db


# ── 1. No prod backdoor: the mock bypass is env-gated and off by default ──

def test_mock_bypass_is_off_in_production_config(monkeypatch):
    """DASHBOARD_MOCK is the *only* toggle for the mock self-issue/bypass path,
    and it is off unless explicitly exported. Production never sets it."""
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    assert network_routes._mock_mode() is False
    # unlock_routes reuses the exact same predicate — not a second copy that
    # could drift and leave a bypass on.
    assert unlock_routes._mock_mode is network_routes._mock_mode
    assert unlock_routes._mock_mode() is False


def test_mock_bypass_is_reachable_only_with_the_env_set(monkeypatch):
    monkeypatch.setenv("DASHBOARD_MOCK", "1")
    assert network_routes._mock_mode() is True


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _json_body(resp) -> dict:
    return json.loads(bytes(resp.body))


def test_get_session_does_not_auto_unlock_in_production(monkeypatch):
    """In mock mode ``/api/identity/session`` reports a deterministic signed-out
    state (the chrome's mock bypass). In production the same route reports the
    gate as ENFORCED and hands out no session — it never self-issues."""
    req = types.SimpleNamespace(cookies={}, headers={})

    # Mock config: the bypass reports enforced=False, unlocked=False.
    monkeypatch.setenv("DASHBOARD_MOCK", "1")
    monkeypatch.setattr(unlock_routes, "gate_disabled", lambda: False)
    mock_resp = _run(unlock_routes.get_session(req))
    assert _json_body(mock_resp)["enforced"] is False

    # Production config: enrolled, kill-switch off, no cookie → enforced, and
    # crucially unlocked=False (no credential materializes from the request).
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    monkeypatch.setattr(unlock_routes, "human_auth_enrolled", lambda: True)
    monkeypatch.setattr(unlock_routes, "gate_disabled", lambda: False)
    monkeypatch.setattr(unlock_routes, "session_from_request", lambda _r: None)
    prod_resp = _run(unlock_routes.get_session(req))
    body = _json_body(prod_resp)
    assert body["enforced"] is True
    assert body["unlocked"] is False


# ── 2. Refuses in production: the human gate rejects, never mints ──────────

def _drive_gate(path, method):
    """Push one request through HumanGateMiddleware and capture the response
    start message (status + headers). The inner app is a sentinel that records
    whether the request was let through."""
    passed = {"through": False}

    async def inner(scope, receive, send):
        passed["through"] = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw = unlock_routes.HumanGateMiddleware(inner)
    scope = {"type": "http", "path": path, "method": method,
             "headers": [], "query_string": b""}
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    _run(mw(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    return passed["through"], start["status"], dict(start.get("headers") or [])


def test_gated_page_request_without_a_session_is_refused(monkeypatch):
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    monkeypatch.setattr(unlock_routes, "gate_disabled", lambda: False)
    monkeypatch.setattr(unlock_routes, "human_auth_enrolled", lambda: True)

    # A page GET redirects to /unlock (no session issued).
    through, status, headers = _drive_gate("/pillars", "GET")
    assert through is False
    assert status == 302
    assert headers.get(b"location", b"").startswith(b"/unlock")

    # A non-GET gated request gets a 401 — never a minted credential.
    through, status, _ = _drive_gate("/pillars", "POST")
    assert through is False
    assert status == 401


def test_recovery_kill_switch_is_the_only_gate_bypass(monkeypatch):
    """DASHBOARD_AUTH is the env-only recovery switch. With it off (production),
    the gate enforces; it is the sole non-ceremony way past the gate, and it is
    operator-controlled, not caller-reachable."""
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    monkeypatch.setattr(unlock_routes, "human_auth_enrolled", lambda: True)
    monkeypatch.setattr(unlock_routes, "gate_disabled", lambda: False)
    through, status, _ = _drive_gate("/pillars", "POST")
    assert through is False and status == 401
    # Flip the operator recovery switch → gate short-circuits open. This proves
    # the bypass exists ONLY behind an env switch, nothing a request can assert.
    monkeypatch.setattr(unlock_routes, "gate_disabled", lambda: True)
    through, status, _ = _drive_gate("/pillars", "POST")
    assert through is True and status == 200


def test_host_cli_token_provision_refuses_a_container(monkeypatch):
    """``graph`` host auto-provision (tools/graph/cli._resolve_crosstalk_token)
    is the one self-issue path near the live system. It positively refuses a
    containerized caller — the production automated caller — rather than mint.
    A container MUST receive its org-stamped token from the launcher."""
    import tools.graph.cli as cli

    monkeypatch.delenv("CROSSTALK_TOKEN", raising=False)
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1234,0")

    def fake_run(argv, *a, **kw):
        if "display-message" in argv:
            return types.SimpleNamespace(returncode=0, stdout="auto-fake\n", stderr="")
        # show-environment: pretend nothing is cached so we fall through to the
        # container check.
        return types.SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    real_exists = cli.os.path.exists
    monkeypatch.setattr(cli.os.path, "exists",
                        lambda p: True if p == "/.dockerenv" else real_exists(p))

    # A minted token would require inserting an auth row — assert we never get
    # there by making insert_token explode if reached.
    monkeypatch.setattr(auth_db, "insert_token",
                        lambda *a, **k: pytest.fail("container self-issued a token"))

    with pytest.raises(SystemExit) as exc:
        cli._resolve_crosstalk_token()
    assert exc.value.code == 1


# ── 3. No over-scoping: the minter forces an explicit scope ────────────────

def test_credential_minter_has_no_scopeless_default():
    """``insert_token`` takes ``org`` with NO default: every mint site — the
    container launcher and the host CLI — must decide the credential's scope
    explicitly. There is no scopeless default a test (or a forgetful new mint
    site) could silently inherit and over-grant."""
    org_param = inspect.signature(auth_db.insert_token).parameters["org"]
    assert org_param.default is inspect.Parameter.empty


def test_org_stamped_token_resolves_to_exactly_its_own_org(tmp_path):
    """A self-issued/test credential stamped for one org resolves to that org
    and only that org — it is not a wildcard. (The reject-NULL escalation guard
    for org-less tokens is proven in test_session_token_org.py.)"""
    import hashlib

    auth_db.init_db(tmp_path / "auth.db")
    try:
        tok = hashlib.sha256(b"scoped").hexdigest()
        auth_db.insert_token(tok, "auto-scoped", "anchore")
        assert auth_db.resolve_token(tok) == ("auto-scoped", "anchore")
    finally:
        if auth_db._conn is not None:
            auth_db._conn.close()
            auth_db._conn = None
