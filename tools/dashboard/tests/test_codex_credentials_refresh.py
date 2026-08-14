"""Tests for the host-side Codex OAuth refresh poller (bead auto-l1h3f).

Mocks ``urllib.request.urlopen`` at the helper boundary so the assertions
exercise classification, payload construction, upsert wiring, and
revoked-row skip behaviour without real auth.openai.com round-trips.
Parity reference: ``test_claude_credentials_refresh.py``.
"""

from __future__ import annotations

import base64
import io
import json
import urllib.error
import urllib.parse
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tools.dashboard import codex_credentials_refresh as crr
from tools.graph.codex_oauth import (
    CODEX_CLIENT_ID,
    CODEX_REFRESH_SCOPE,
    CODEX_TOKEN_URL,
)


# ── JWT + HTTP boundary helpers ──────────────────────────────


def _jwt(exp: int | None) -> str:
    """Build a structurally-valid JWT whose payload carries ``exp`` (or none)."""
    claims: dict = {}
    if exp is not None:
        claims["exp"] = exp
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(
        json.dumps(claims).encode("utf-8")
    ).decode().rstrip("=")
    return f"{header}.{payload}.sig"


class _FakeResp:
    def __init__(self, payload: dict | str, *, status: int = 200):
        if isinstance(payload, dict):
            self._body = json.dumps(payload).encode("utf-8")
        else:
            self._body = payload.encode("utf-8")
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _success_body(*, access="at-NEW", refresh="rt-NEW", exp=4102444800):
    # exp default = 2100-01-01, comfortably in the future.
    return {
        "access_token": access,
        "refresh_token": refresh,
        "id_token": _jwt(exp),
        "token_type": "Bearer",
        "expires_in": 864000,
        "scope": CODEX_REFRESH_SCOPE,
    }


def _http_error(status: int, body: dict):
    return urllib.error.HTTPError(
        CODEX_TOKEN_URL,
        status,
        f"{status}",
        {},
        io.BytesIO(json.dumps(body).encode("utf-8")),
    )


# ── refresh_one ──────────────────────────────────────────────


def test_refresh_one_success_returns_ok_with_rotated_triple():
    captured: dict = {}

    def fake_urlopen(req, timeout=None, context=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["body"] = urllib.parse.parse_qs(req.data.decode("utf-8"))
        captured["content_type"] = req.headers.get("Content-type")
        return _FakeResp(_success_body(exp=4102444800))

    with patch("urllib.request.urlopen", fake_urlopen):
        result = crr.refresh_one("rt-OLD")

    assert captured["url"] == CODEX_TOKEN_URL
    assert captured["method"] == "POST"
    # Standard OAuth form-encoded token request, with offline_access scope.
    assert captured["content_type"] == "application/x-www-form-urlencoded"
    assert captured["body"]["grant_type"] == ["refresh_token"]
    assert captured["body"]["client_id"] == [CODEX_CLIENT_ID]
    assert captured["body"]["refresh_token"] == ["rt-OLD"]
    assert "offline_access" in captured["body"]["scope"][0]

    assert result.kind == "ok"
    assert result.access_token == "at-NEW"
    assert result.refresh_token == "rt-NEW"
    assert result.id_token is not None
    assert result.expires_at_ms == 4102444800 * 1000


def test_refresh_one_invalid_grant_is_revoked():
    def fake_urlopen(req, timeout=None, context=None):
        raise _http_error(400, {"error": "invalid_grant",
                                 "error_description": "token revoked"})

    with patch("urllib.request.urlopen", fake_urlopen):
        result = crr.refresh_one("rt-DEAD")
    assert result.kind == "revoked"
    assert result.error.startswith("invalid_grant")


def test_refresh_one_refresh_token_reused_is_revoked():
    def fake_urlopen(req, timeout=None, context=None):
        raise _http_error(400, {"error": "refresh_token_reused"})

    with patch("urllib.request.urlopen", fake_urlopen):
        result = crr.refresh_one("rt-REUSED")
    assert result.kind == "revoked"


def test_refresh_one_401_is_revoked():
    def fake_urlopen(req, timeout=None, context=None):
        raise _http_error(401, {"error": "unauthorized"})

    with patch("urllib.request.urlopen", fake_urlopen):
        result = crr.refresh_one("rt-X")
    assert result.kind == "revoked"


def test_refresh_one_429_is_transient():
    def fake_urlopen(req, timeout=None, context=None):
        raise _http_error(429, {"error": "rate_limited"})

    with patch("urllib.request.urlopen", fake_urlopen):
        result = crr.refresh_one("rt-X")
    assert result.kind == "transient"
    assert "429" in result.error


def test_refresh_one_network_error_is_transient():
    def fake_urlopen(req, timeout=None, context=None):
        raise urllib.error.URLError("connection refused")

    with patch("urllib.request.urlopen", fake_urlopen):
        result = crr.refresh_one("rt-X")
    assert result.kind == "transient"
    assert "network" in result.error


def test_refresh_one_missing_refresh_token_is_transient():
    body = _success_body()
    del body["refresh_token"]

    def fake_urlopen(req, timeout=None, context=None):
        return _FakeResp(body)

    with patch("urllib.request.urlopen", fake_urlopen):
        result = crr.refresh_one("rt-X")
    assert result.kind == "transient"
    assert "refresh_token" in result.error


def test_refresh_one_missing_id_token_is_transient():
    body = _success_body()
    del body["id_token"]

    def fake_urlopen(req, timeout=None, context=None):
        return _FakeResp(body)

    with patch("urllib.request.urlopen", fake_urlopen):
        result = crr.refresh_one("rt-X")
    assert result.kind == "transient"
    assert "id_token" in result.error


def test_refresh_one_id_token_without_exp_is_transient():
    body = _success_body()
    body["id_token"] = _jwt(None)

    def fake_urlopen(req, timeout=None, context=None):
        return _FakeResp(body)

    with patch("urllib.request.urlopen", fake_urlopen):
        result = crr.refresh_one("rt-X")
    assert result.kind == "transient"
    assert "exp" in result.error


# ── _needs_refresh ───────────────────────────────────────────


def test_needs_refresh_within_threshold():
    now = 1_000_000_000_000
    # 1 day of TTL left — under the 3-day threshold → needs refresh.
    payload = {"expires_at_ms": now + 24 * 60 * 60 * 1000}
    assert crr._needs_refresh(payload, now_ms=now) is True


def test_needs_refresh_outside_threshold():
    now = 1_000_000_000_000
    # 9 days of TTL left → no refresh yet.
    payload = {"expires_at_ms": now + 9 * 24 * 60 * 60 * 1000}
    assert crr._needs_refresh(payload, now_ms=now) is False


def test_needs_refresh_missing_expiry_defaults_true():
    assert crr._needs_refresh({}, now_ms=123) is True
    assert crr._needs_refresh({"expires_at_ms": "nope"}, now_ms=123) is True


# ── refresh_credential_row ───────────────────────────────────


def _row(key: str, payload: dict):
    return SimpleNamespace(key=key, payload=payload)


def test_row_skips_revoked_without_network():
    row = _row("acct-1", {
        "refresh_token": "rt", "expires_at_ms": 0,
        "last_refresh_error": "invalid_grant: gone",
    })

    def boom(*a, **k):  # network must not be touched
        raise AssertionError("should not refresh a revoked row")

    with patch.object(crr, "refresh_one", boom):
        out = crr.refresh_credential_row(
            row, org="personal", now_ms=10, now_iso="now",
        )
    assert out is None


def test_row_skips_fresh_row():
    now = 1_000_000_000_000
    row = _row("acct-1", {
        "refresh_token": "rt",
        "expires_at_ms": now + 9 * 24 * 60 * 60 * 1000,
    })

    def boom(*a, **k):
        raise AssertionError("should not refresh a fresh row")

    with patch.object(crr, "refresh_one", boom):
        out = crr.refresh_credential_row(
            row, org="personal", now_ms=now, now_iso="now",
        )
    assert out is None


def test_row_skips_when_no_refresh_token():
    now = 1_000_000_000_000
    row = _row("acct-1", {"expires_at_ms": now})
    out = crr.refresh_credential_row(
        row, org="personal", now_ms=now, now_iso="now",
    )
    assert out is None


def test_row_success_upserts_rotated_payload_preserving_identity():
    now = 1_000_000_000_000
    row = _row("acct-1", {
        "email": "user@example.com",
        "auth_mode": "chatgpt",
        "access_token": "at-OLD",
        "refresh_token": "rt-OLD",
        "id_token": "id-OLD",
        "expires_at_ms": now + 1000,  # stale
        "last_refresh_error": "HTTP 429: earlier blip",
    })
    captured: dict = {}

    def fake_upsert(set_id, rev, key, payload, *, org):
        captured.update(set_id=set_id, rev=rev, key=key,
                        payload=payload, org=org)

    with patch.object(crr, "refresh_one",
                      return_value=crr.RefreshResult(
                          kind="ok", access_token="at-NEW",
                          refresh_token="rt-NEW", id_token="id-NEW",
                          expires_at_ms=now + 999_999)):
        with patch.object(crr.graph_ops, "upsert_by_key", fake_upsert):
            out = crr.refresh_credential_row(
                row, org="personal", now_ms=now, now_iso="2026-08-14T00:00:00Z",
            )
    assert out.kind == "ok"
    p = captured["payload"]
    assert captured["key"] == "acct-1"
    assert p["access_token"] == "at-NEW"
    assert p["refresh_token"] == "rt-NEW"
    assert p["id_token"] == "id-NEW"
    assert p["expires_at_ms"] == now + 999_999
    assert p["last_refresh_at"] == "2026-08-14T00:00:00Z"
    # identity fields survive; stale error is cleared on success
    assert p["email"] == "user@example.com"
    assert p["auth_mode"] == "chatgpt"
    assert "last_refresh_error" not in p


def test_row_revoked_stamps_error_and_keeps_tokens():
    now = 1_000_000_000_000
    row = _row("acct-1", {
        "auth_mode": "chatgpt",
        "access_token": "at-OLD",
        "refresh_token": "rt-OLD",
        "id_token": "id-OLD",
        "expires_at_ms": now + 1000,
    })
    captured: dict = {}

    def fake_upsert(set_id, rev, key, payload, *, org):
        captured.update(payload=payload)

    with patch.object(crr, "refresh_one",
                      return_value=crr.RefreshResult(
                          kind="revoked", error="invalid_grant: revoked")):
        with patch.object(crr.graph_ops, "upsert_by_key", fake_upsert):
            out = crr.refresh_credential_row(
                row, org="personal", now_ms=now, now_iso="now",
            )
    assert out.kind == "revoked"
    p = captured["payload"]
    # tokens untouched, error stamped so a later tick skips it
    assert p["access_token"] == "at-OLD"
    assert p["refresh_token"] == "rt-OLD"
    assert p["last_refresh_error"].startswith("invalid_grant")


# ── refresh_all_credentials ──────────────────────────────────


def test_refresh_all_counts_outcomes():
    now_far = 4102444800 * 1000  # very fresh
    rows = [
        _row("fresh", {"refresh_token": "rt", "expires_at_ms": now_far}),
        _row("stale", {"refresh_token": "rt", "expires_at_ms": 1}),
        _row("revoked", {"refresh_token": "rt", "expires_at_ms": 1,
                          "last_refresh_error": "invalid_grant: x"}),
    ]
    members = SimpleNamespace(members=rows)

    def fake_read_set(set_id, *, org, peers=None):
        return members

    def fake_upsert(*a, **k):
        pass

    with patch.object(crr.graph_ops, "read_set", fake_read_set):
        with patch.object(crr.graph_ops, "upsert_by_key", fake_upsert):
            with patch.object(crr, "refresh_one",
                              return_value=crr.RefreshResult(
                                  kind="ok", access_token="a",
                                  refresh_token="r", id_token="i",
                                  expires_at_ms=now_far)):
                counters = crr.refresh_all_credentials()
    assert counters["ok"] == 1        # the stale row refreshed
    assert counters["skipped"] == 2   # fresh + revoked skipped


def test_refresh_all_read_set_failure_returns_zero_counters():
    def boom(*a, **k):
        raise RuntimeError("db down")

    with patch.object(crr.graph_ops, "read_set", boom):
        counters = crr.refresh_all_credentials()
    assert counters == {"ok": 0, "revoked": 0, "transient": 0, "skipped": 0}


def test_refresh_all_empty_set_warns_with_remedy(caplog):
    # A Codex-enabled fleet with zero credential rows is the exact outage
    # signature — it must WARN (not INFO) and name the remedy inline.
    with caplog.at_level("INFO", logger=crr.logger.name):
        with patch.object(crr.graph_ops, "read_set",
                          return_value=SimpleNamespace(members=[])):
            counters = crr.refresh_all_credentials()
    assert counters == {"ok": 0, "revoked": 0, "transient": 0, "skipped": 0}
    warns = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warns) == 1
    msg = warns[0].getMessage()
    assert "ZERO rows" in msg
    assert "graph credentials import" in msg


def test_refresh_all_read_set_failure_does_not_warn_zero_rows(caplog):
    # A read_set failure already logs an exception; it must NOT masquerade
    # as the zero-row WARN, which means "the surface is genuinely empty".
    def boom(*a, **k):
        raise RuntimeError("db down")

    with caplog.at_level("INFO", logger=crr.logger.name):
        with patch.object(crr.graph_ops, "read_set", boom):
            crr.refresh_all_credentials()
    warns = [r for r in caplog.records if r.levelname == "WARNING"]
    assert not any("ZERO rows" in r.getMessage() for r in warns)


def test_refresh_all_tick_log_states_decision_basis(caplog):
    now_far = 4102444800 * 1000  # very fresh
    rows = [
        _row("fresh", {"refresh_token": "rt", "expires_at_ms": now_far}),
        _row("stale", {"refresh_token": "rt", "expires_at_ms": 1}),
    ]
    members = SimpleNamespace(members=rows)

    with caplog.at_level("INFO", logger=crr.logger.name):
        with patch.object(crr.graph_ops, "read_set",
                          return_value=members):
            with patch.object(crr.graph_ops, "upsert_by_key", lambda *a, **k: None):
                with patch.object(crr, "refresh_one",
                                  return_value=crr.RefreshResult(
                                      kind="ok", access_token="a",
                                      refresh_token="r", id_token="i",
                                      expires_at_ms=now_far)):
                    crr.refresh_all_credentials()
    tick = [r.getMessage() for r in caplog.records
            if "refresh tick:" in r.getMessage()]
    assert len(tick) == 1
    # Not bare counters — the decision basis is spelled out.
    assert "2 row(s) found" in tick[0]
    assert "1 refreshed" in tick[0]
    assert "no standing failures" in tick[0]


def test_refresh_all_tick_log_reports_standing_failure_age(caplog):
    now = 1_000_000_000_000
    # A row that last succeeded long ago and now carries a standing error.
    failing = _row("bad", {
        "refresh_token": "rt", "expires_at_ms": now + 9 * 24 * 3600 * 1000,
        "last_refresh_error": "HTTP 429: blip",
        "last_refresh_at": "2000-01-01T00:00:00Z",
    })
    members = SimpleNamespace(members=[failing])
    with caplog.at_level("INFO", logger=crr.logger.name):
        with patch.object(crr.graph_ops, "read_set",
                          return_value=members):
            crr.refresh_all_credentials()
    tick = [r.getMessage() for r in caplog.records
            if "refresh tick:" in r.getMessage()]
    assert len(tick) == 1
    assert "1 standing failure(s)" in tick[0]
    assert "since last success" in tick[0]


# ── poller gate ──────────────────────────────────────────────


def test_should_run_gate_skips_under_pytest():
    # PYTEST_CURRENT_TEST is set inside a running test → gate returns False.
    assert crr.should_run_codex_credentials_refresh_poller() is False
