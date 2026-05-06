"""Tests for the host-side OAuth refresh poller (graph://73c4e9ef-bbc, bead 3).

Mocks ``urllib.request.urlopen`` at the helper boundary so the assertions
exercise classification, payload construction, upsert wiring, and
revoked-row skip behaviour without real Anthropic round-trips.
"""

from __future__ import annotations

import io
import json
import logging
import urllib.error
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tools.dashboard import claude_credentials_refresh as crr
from tools.graph.claude_oauth import CLIENT_ID, TOKEN_URL


# ── HTTP boundary helpers ────────────────────────────────────


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


def _success_body(*, access="at-NEW", refresh="rt-NEW", expires_in=28800):
    return {
        "access_token": access,
        "refresh_token": refresh,
        "expires_in": expires_in,
        "token_type": "Bearer",
        "scope": "user:profile user:inference",
    }


def _http_error(status: int, body: dict):
    return urllib.error.HTTPError(
        TOKEN_URL,
        status,
        f"{status}",
        {},
        io.BytesIO(json.dumps(body).encode("utf-8")),
    )


# ── refresh_one ──────────────────────────────────────────────


def test_refresh_one_success_returns_ok_with_rotated_tokens():
    captured: dict = {}

    def fake_urlopen(req, timeout=None, context=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["content_type"] = req.headers.get("Content-type")
        return _FakeResp(_success_body())

    with patch("urllib.request.urlopen", fake_urlopen):
        result = crr.refresh_one("rt-OLD")

    assert captured["url"] == TOKEN_URL
    assert captured["method"] == "POST"
    assert captured["content_type"] == "application/json"
    assert captured["body"] == {
        "grant_type": "refresh_token",
        "refresh_token": "rt-OLD",
        "client_id": CLIENT_ID,
    }
    assert result.kind == "ok"
    assert result.access_token == "at-NEW"
    assert result.refresh_token == "rt-NEW"
    assert result.expires_in == 28800
    assert result.error is None


def test_refresh_one_401_invalid_grant_classified_as_revoked():
    def fake_urlopen(req, timeout=None, context=None):
        raise _http_error(401, {
            "error": "invalid_grant",
            "error_description": "refresh_token revoked",
        })

    with patch("urllib.request.urlopen", fake_urlopen):
        result = crr.refresh_one("rt-DEAD")

    assert result.kind == "revoked"
    assert result.error.startswith(crr.INVALID_GRANT_PREFIX)
    assert "refresh_token revoked" in result.error


def test_refresh_one_invalid_grant_400_classified_as_revoked():
    """Anthropic also returns 400 + invalid_grant in some flows.

    Test that the ``error == "invalid_grant"`` branch fires regardless of
    status code, so we don't loop on a dead refresh_token.
    """
    def fake_urlopen(req, timeout=None, context=None):
        raise _http_error(400, {"error": "invalid_grant"})

    with patch("urllib.request.urlopen", fake_urlopen):
        result = crr.refresh_one("rt-DEAD")

    assert result.kind == "revoked"
    assert result.error.startswith(crr.INVALID_GRANT_PREFIX)


def test_refresh_one_429_classified_as_transient():
    def fake_urlopen(req, timeout=None, context=None):
        raise _http_error(429, {
            "error": "rate_limited",
            "error_description": "too many requests",
        })

    with patch("urllib.request.urlopen", fake_urlopen):
        result = crr.refresh_one("rt-OK")

    assert result.kind == "transient"
    assert "429" in result.error


def test_refresh_one_network_error_classified_as_transient():
    def fake_urlopen(req, timeout=None, context=None):
        raise urllib.error.URLError("connection refused")

    with patch("urllib.request.urlopen", fake_urlopen):
        result = crr.refresh_one("rt-OK")

    assert result.kind == "transient"
    assert "network" in result.error


def test_refresh_one_2xx_missing_access_token_is_transient():
    def fake_urlopen(req, timeout=None, context=None):
        return _FakeResp({
            # access_token missing
            "refresh_token": "rt-NEW",
            "expires_in": 28800,
        })

    with patch("urllib.request.urlopen", fake_urlopen):
        result = crr.refresh_one("rt-OK")

    assert result.kind == "transient"
    assert "access_token" in result.error


def test_refresh_one_2xx_missing_expires_in_is_transient():
    def fake_urlopen(req, timeout=None, context=None):
        return _FakeResp({
            "access_token": "at-NEW",
            "refresh_token": "rt-NEW",
            # expires_in missing
        })

    with patch("urllib.request.urlopen", fake_urlopen):
        result = crr.refresh_one("rt-OK")

    assert result.kind == "transient"
    assert "expires_in" in result.error


# ── _needs_refresh / _is_revoked ─────────────────────────────


def test_needs_refresh_skips_when_more_than_4h_remaining():
    now_ms = 1_700_000_000_000
    payload = {"expires_at_ms": now_ms + 5 * 3600 * 1000}  # 5h remaining
    assert crr._needs_refresh(payload, now_ms=now_ms) is False


def test_needs_refresh_fires_when_within_4h_window():
    now_ms = 1_700_000_000_000
    payload = {"expires_at_ms": now_ms + 3 * 3600 * 1000}  # 3h remaining
    assert crr._needs_refresh(payload, now_ms=now_ms) is True


def test_needs_refresh_fires_when_already_expired():
    now_ms = 1_700_000_000_000
    payload = {"expires_at_ms": now_ms - 1}
    assert crr._needs_refresh(payload, now_ms=now_ms) is True


def test_needs_refresh_treats_missing_expires_as_stale():
    now_ms = 1_700_000_000_000
    assert crr._needs_refresh({}, now_ms=now_ms) is True
    assert crr._needs_refresh({"expires_at_ms": "nope"}, now_ms=now_ms) is True


def test_is_revoked_only_matches_invalid_grant_prefix():
    assert crr._is_revoked({
        "last_refresh_error": "invalid_grant: revoked",
    }) is True
    assert crr._is_revoked({
        "last_refresh_error": "HTTP 429: rate limited",
    }) is False
    assert crr._is_revoked({}) is False


# ── _build_payload_after_refresh ─────────────────────────────


def _base_payload() -> dict:
    return {
        "alias": "gmail-max",
        "organization_name": "Example Org",
        "account_email": "max@example.com",
        "access_token": "at-OLD",
        "refresh_token": "rt-OLD",
        "expires_at_ms": 1_700_000_000_000,
        "scopes": [
            "user:profile",
            "user:inference",
            "user:sessions:claude_code",
        ],
    }


def test_build_payload_on_success_rotates_tokens_and_clears_error():
    base = _base_payload()
    base["last_refresh_error"] = "HTTP 429: too many requests"
    result = crr.RefreshResult(
        kind="ok", access_token="at-NEW", refresh_token="rt-NEW",
        expires_in=28800,
    )
    payload = crr._build_payload_after_refresh(
        base=base, result=result,
        now_ms=1_700_000_000_000, now_iso="2026-05-06T01:00:00Z",
    )
    assert payload["access_token"] == "at-NEW"
    assert payload["refresh_token"] == "rt-NEW"
    assert payload["expires_at_ms"] == 1_700_000_000_000 + 28800 * 1000
    assert payload["last_refresh_at"] == "2026-05-06T01:00:00Z"
    assert "last_refresh_error" not in payload
    assert payload["alias"] == "gmail-max"


def test_build_payload_on_failure_preserves_tokens_and_stamps_error():
    base = _base_payload()
    result = crr.RefreshResult(kind="transient", error="HTTP 429: rl")
    payload = crr._build_payload_after_refresh(
        base=base, result=result,
        now_ms=1_700_000_000_000, now_iso="2026-05-06T01:00:00Z",
    )
    assert payload["access_token"] == "at-OLD"
    assert payload["refresh_token"] == "rt-OLD"
    assert payload["expires_at_ms"] == 1_700_000_000_000
    assert payload["last_refresh_error"] == "HTTP 429: rl"
    # last_refresh_at is preserved from the base if it was there;
    # since base has none, it should not appear on a failure either.
    assert "last_refresh_at" not in payload


def test_build_payload_on_failure_keeps_prior_last_refresh_at():
    base = _base_payload()
    base["last_refresh_at"] = "2026-05-05T20:00:00Z"
    result = crr.RefreshResult(kind="transient", error="HTTP 429: rl")
    payload = crr._build_payload_after_refresh(
        base=base, result=result,
        now_ms=1_700_000_000_000, now_iso="2026-05-06T01:00:00Z",
    )
    assert payload["last_refresh_at"] == "2026-05-05T20:00:00Z"
    assert payload["last_refresh_error"] == "HTTP 429: rl"


# ── refresh_credential_row ───────────────────────────────────


def _row(*, key: str, payload: dict) -> SimpleNamespace:
    return SimpleNamespace(key=key, payload=payload)


def test_refresh_credential_row_skips_revoked_rows(monkeypatch):
    upserts: list = []
    refresh_calls: list = []

    monkeypatch.setattr(
        crr.graph_ops, "upsert_by_key",
        lambda *a, **kw: upserts.append((a, kw)),
    )
    monkeypatch.setattr(
        crr, "refresh_one",
        lambda tok: refresh_calls.append(tok) or None,
    )

    payload = _base_payload()
    payload["last_refresh_error"] = "invalid_grant: revoked"
    # Even with stale expiry, we must NOT call the API for a revoked row.
    payload["expires_at_ms"] = 0

    row = _row(key="org-1", payload=payload)
    result = crr.refresh_credential_row(
        row, org="autonomy", now_ms=1_700_000_000_000,
        now_iso="2026-05-06T01:00:00Z",
    )

    assert result is None
    assert refresh_calls == []
    assert upserts == []


def test_refresh_credential_row_skips_fresh_rows(monkeypatch):
    upserts: list = []
    refresh_calls: list = []
    monkeypatch.setattr(
        crr.graph_ops, "upsert_by_key",
        lambda *a, **kw: upserts.append((a, kw)),
    )
    monkeypatch.setattr(
        crr, "refresh_one",
        lambda tok: refresh_calls.append(tok) or None,
    )

    payload = _base_payload()
    now_ms = 1_700_000_000_000
    # 5h of remaining lifetime — well past the 4h threshold.
    payload["expires_at_ms"] = now_ms + 5 * 3600 * 1000

    row = _row(key="org-1", payload=payload)
    result = crr.refresh_credential_row(
        row, org="autonomy", now_ms=now_ms, now_iso="2026-05-06T01:00:00Z",
    )

    assert result is None
    assert refresh_calls == []
    assert upserts == []


def test_refresh_credential_row_writes_rotated_tokens_on_success(monkeypatch):
    upserts: list = []

    def fake_upsert(set_id, schema_revision, key, payload, *, org, state="raw"):
        upserts.append({
            "set_id": set_id,
            "schema_revision": schema_revision,
            "key": key,
            "payload": payload,
            "org": org,
        })
        return "sid-1"

    monkeypatch.setattr(crr.graph_ops, "upsert_by_key", fake_upsert)
    monkeypatch.setattr(
        crr, "refresh_one",
        lambda tok: crr.RefreshResult(
            kind="ok", access_token="at-NEW", refresh_token="rt-NEW",
            expires_in=28800,
        ),
    )

    payload = _base_payload()
    now_ms = 1_700_000_000_000
    payload["expires_at_ms"] = now_ms + 1 * 3600 * 1000  # within window

    row = _row(key="org-1", payload=payload)
    result = crr.refresh_credential_row(
        row, org="autonomy", now_ms=now_ms, now_iso="2026-05-06T01:00:00Z",
    )

    assert result.kind == "ok"
    assert len(upserts) == 1
    upsert = upserts[0]
    assert upsert["set_id"] == crr.CLAUDE_CREDENTIALS_SET_ID
    assert upsert["schema_revision"] == crr.CLAUDE_CREDENTIALS_REVISION
    assert upsert["key"] == "org-1"
    assert upsert["org"] == "autonomy"
    assert upsert["payload"]["access_token"] == "at-NEW"
    assert upsert["payload"]["refresh_token"] == "rt-NEW"
    assert upsert["payload"]["expires_at_ms"] == now_ms + 28800 * 1000
    assert upsert["payload"]["last_refresh_at"] == "2026-05-06T01:00:00Z"
    assert "last_refresh_error" not in upsert["payload"]


def test_refresh_credential_row_preserves_tokens_on_invalid_grant(
    monkeypatch, caplog,
):
    upserts: list = []
    monkeypatch.setattr(
        crr.graph_ops, "upsert_by_key",
        lambda set_id, schema_revision, key, payload, *, org, state="raw":
            upserts.append(payload),
    )
    monkeypatch.setattr(
        crr, "refresh_one",
        lambda tok: crr.RefreshResult(
            kind="revoked", error="invalid_grant: refresh_token revoked",
        ),
    )

    payload = _base_payload()
    now_ms = 1_700_000_000_000
    payload["expires_at_ms"] = now_ms + 1 * 3600 * 1000  # in window

    row = _row(key="org-1", payload=payload)
    with caplog.at_level(logging.WARNING, logger=crr.logger.name):
        result = crr.refresh_credential_row(
            row, org="autonomy", now_ms=now_ms, now_iso="2026-05-06T01:00:00Z",
        )

    assert result.kind == "revoked"
    assert len(upserts) == 1
    written = upserts[0]
    # Tokens preserved.
    assert written["access_token"] == "at-OLD"
    assert written["refresh_token"] == "rt-OLD"
    assert written["expires_at_ms"] == now_ms + 1 * 3600 * 1000
    # Sentinel error stamped.
    assert written["last_refresh_error"].startswith(crr.INVALID_GRANT_PREFIX)
    # Operator-actionable message logged. The bead requires the message
    # tells the operator to re-run `graph claude install`.
    log_text = " ".join(rec.getMessage() for rec in caplog.records)
    assert "invalid_grant" in log_text
    assert "graph claude install" in log_text
    assert "gmail-max" in log_text  # alias surfaced


def test_refresh_credential_row_preserves_tokens_on_transient(monkeypatch):
    upserts: list = []
    monkeypatch.setattr(
        crr.graph_ops, "upsert_by_key",
        lambda set_id, schema_revision, key, payload, *, org, state="raw":
            upserts.append(payload),
    )
    monkeypatch.setattr(
        crr, "refresh_one",
        lambda tok: crr.RefreshResult(
            kind="transient", error="HTTP 429: rate-limited",
        ),
    )

    payload = _base_payload()
    now_ms = 1_700_000_000_000
    payload["expires_at_ms"] = now_ms + 1 * 3600 * 1000

    row = _row(key="org-1", payload=payload)
    result = crr.refresh_credential_row(
        row, org="autonomy", now_ms=now_ms, now_iso="2026-05-06T01:00:00Z",
    )

    assert result.kind == "transient"
    assert len(upserts) == 1
    written = upserts[0]
    assert written["access_token"] == "at-OLD"
    assert written["refresh_token"] == "rt-OLD"
    assert written["last_refresh_error"] == "HTTP 429: rate-limited"


def test_refresh_credential_row_skips_row_without_refresh_token(monkeypatch):
    upserts: list = []
    refresh_calls: list = []
    monkeypatch.setattr(
        crr.graph_ops, "upsert_by_key",
        lambda *a, **kw: upserts.append((a, kw)),
    )
    monkeypatch.setattr(
        crr, "refresh_one",
        lambda tok: refresh_calls.append(tok) or None,
    )

    payload = _base_payload()
    payload["refresh_token"] = ""  # legacy / corrupt row
    now_ms = 1_700_000_000_000
    payload["expires_at_ms"] = now_ms - 1  # would be stale

    row = _row(key="org-1", payload=payload)
    result = crr.refresh_credential_row(
        row, org="autonomy", now_ms=now_ms, now_iso="2026-05-06T01:00:00Z",
    )

    assert result is None
    assert refresh_calls == []
    assert upserts == []


# ── refresh_all_credentials ──────────────────────────────────


def _stub_read_set(monkeypatch, rows):
    monkeypatch.setattr(
        crr.graph_ops, "read_set",
        lambda *a, **kw: SimpleNamespace(members=rows),
    )


def test_refresh_all_credentials_counts_per_outcome(monkeypatch):
    upserts: list = []

    fresh = _row(key="org-fresh", payload={
        **_base_payload(),
        "expires_at_ms": 1_700_000_000_000 + 10 * 3600 * 1000,
    })
    revoked = _row(key="org-rev", payload={
        **_base_payload(),
        "alias": "rev",
        "expires_at_ms": 0,
        "last_refresh_error": "invalid_grant: dead",
    })
    stale_ok = _row(key="org-ok", payload={
        **_base_payload(),
        "alias": "ok",
        "expires_at_ms": 1_700_000_000_000 + 1 * 3600 * 1000,
    })
    stale_429 = _row(key="org-429", payload={
        **_base_payload(),
        "alias": "rate",
        "refresh_token": "rt-RATE",
        "expires_at_ms": 1_700_000_000_000 + 1 * 3600 * 1000,
    })
    stale_dead = _row(key="org-dead", payload={
        **_base_payload(),
        "alias": "dead",
        "refresh_token": "rt-DEAD",
        "expires_at_ms": 1_700_000_000_000 + 1 * 3600 * 1000,
    })

    _stub_read_set(monkeypatch, [
        fresh, revoked, stale_ok, stale_429, stale_dead,
    ])
    monkeypatch.setattr(
        crr.graph_ops, "upsert_by_key",
        lambda *a, **kw: upserts.append((a, kw)),
    )
    monkeypatch.setattr(crr, "_now_ms", lambda: 1_700_000_000_000)
    monkeypatch.setattr(crr, "_now_iso", lambda: "2026-05-06T01:00:00Z")

    def fake_refresh_one(tok):
        if tok == "rt-OLD":
            return crr.RefreshResult(
                kind="ok", access_token="at-NEW", refresh_token="rt-NEW",
                expires_in=28800,
            )
        if tok == "rt-RATE":
            return crr.RefreshResult(
                kind="transient", error="HTTP 429: rl",
            )
        if tok == "rt-DEAD":
            return crr.RefreshResult(
                kind="revoked", error="invalid_grant: gone",
            )
        raise AssertionError(f"unexpected token {tok!r}")

    monkeypatch.setattr(crr, "refresh_one", fake_refresh_one)

    counters = crr.refresh_all_credentials()

    assert counters == {"ok": 1, "revoked": 1, "transient": 1, "skipped": 2}
    # Three writes (one per non-skipped row).
    assert len(upserts) == 3


def test_refresh_all_credentials_no_rows_returns_empty_counters(monkeypatch):
    _stub_read_set(monkeypatch, [])
    counters = crr.refresh_all_credentials()
    assert counters == {"ok": 0, "revoked": 0, "transient": 0, "skipped": 0}


def test_refresh_all_credentials_swallows_read_set_failure(monkeypatch, caplog):
    def boom(*a, **kw):
        raise RuntimeError("graph DB locked")

    monkeypatch.setattr(crr.graph_ops, "read_set", boom)
    with caplog.at_level(logging.ERROR, logger=crr.logger.name):
        counters = crr.refresh_all_credentials()
    assert counters == {"ok": 0, "revoked": 0, "transient": 0, "skipped": 0}
    assert any(
        "read_set failed" in rec.getMessage() for rec in caplog.records
    )


def test_refresh_all_credentials_swallows_per_row_exception(monkeypatch):
    upserts: list = []
    rows = [_row(key="org-1", payload={
        **_base_payload(),
        "expires_at_ms": 1_700_000_000_000 + 1 * 3600 * 1000,
    })]
    _stub_read_set(monkeypatch, rows)
    monkeypatch.setattr(
        crr.graph_ops, "upsert_by_key",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(
        crr, "refresh_one",
        lambda tok: crr.RefreshResult(
            kind="ok", access_token="at-NEW", refresh_token="rt-NEW",
            expires_in=28800,
        ),
    )
    monkeypatch.setattr(crr, "_now_ms", lambda: 1_700_000_000_000)
    monkeypatch.setattr(crr, "_now_iso", lambda: "2026-05-06T01:00:00Z")

    counters = crr.refresh_all_credentials()
    assert counters["transient"] == 1


# ── poller env gate ─────────────────────────────────────────


def test_should_run_credentials_refresh_poller_skips_in_mock(monkeypatch):
    monkeypatch.setenv("DASHBOARD_MOCK", "1")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    assert crr.should_run_credentials_refresh_poller() is False


def test_should_run_credentials_refresh_poller_skips_in_pytest(monkeypatch):
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "x")
    assert crr.should_run_credentials_refresh_poller() is False


def test_should_run_credentials_refresh_poller_runs_otherwise(monkeypatch):
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    assert crr.should_run_credentials_refresh_poller() is True


# ── thresholds & integration shape ─────────────────────────


def test_threshold_is_four_hours():
    assert crr.CREDENTIAL_FRESH_THRESHOLD_MS == 4 * 60 * 60 * 1000


def test_poll_interval_is_one_hour():
    assert crr.CREDENTIAL_REFRESH_POLL_INTERVAL == 3600.0


def test_payload_after_refresh_round_trips_through_schema():
    """The upsert payload must satisfy the registered schema."""
    from tools.graph.schemas.claude_credentials import ClaudeCredentialsV1

    base = _base_payload()
    success = crr.RefreshResult(
        kind="ok", access_token="at-NEW", refresh_token="rt-NEW",
        expires_in=28800,
    )
    payload = crr._build_payload_after_refresh(
        base=base, result=success,
        now_ms=1_700_000_000_000, now_iso="2026-05-06T01:00:00Z",
    )
    ClaudeCredentialsV1.validate(payload)

    failure = crr.RefreshResult(kind="transient", error="HTTP 429: rl")
    payload = crr._build_payload_after_refresh(
        base=base, result=failure,
        now_ms=1_700_000_000_000, now_iso="2026-05-06T01:00:00Z",
    )
    ClaudeCredentialsV1.validate(payload)


def test_post_refresh_request_body_is_json(monkeypatch):
    """End-to-end shape check at the urllib boundary."""
    captured: dict = {}

    def fake_urlopen(req, timeout=None, context=None):
        captured["url"] = req.full_url
        captured["data"] = req.data
        captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
        return _FakeResp(_success_body())

    with patch("urllib.request.urlopen", fake_urlopen):
        crr._post_refresh("rt-1234")

    assert captured["url"] == TOKEN_URL
    assert captured["headers"]["content-type"] == "application/json"
    body = json.loads(captured["data"].decode("utf-8"))
    assert body["grant_type"] == "refresh_token"
    assert body["refresh_token"] == "rt-1234"
    assert body["client_id"] == CLIENT_ID
