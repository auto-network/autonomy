"""Tests for the host-side Codex OAuth refresh poller (beads auto-l1h3f,
auto-vqa8n).

Mocks ``urllib.request.urlopen`` at the helper boundary so the assertions
exercise classification, payload construction, upsert wiring, and
revoked-row skip behaviour without real auth.openai.com round-trips.
Parity reference: ``test_claude_credentials_refresh.py``.

The auto-vqa8n additions pin the *freshness honesty* contract: refresh
decisions derive from measured state (last successful refresh + failure
age), never from the ~60-minute id_token exp; the failure-age alarm fires
on the value that predicts a launch failure; and a superseded refresh
token is a distinct, alarmed canary rather than an ordinary revocation.
"""

from __future__ import annotations

import base64
import io
import json
import logging
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


_HOUR_MS = 60 * 60 * 1000
_DAY_MS = 24 * _HOUR_MS


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


def test_refresh_one_refresh_token_reused_is_superseded():
    # The canary code: single-use rotation rejecting an already-used token.
    def fake_urlopen(req, timeout=None, context=None):
        raise _http_error(400, {"error": "refresh_token_reused"})

    with patch("urllib.request.urlopen", fake_urlopen):
        result = crr.refresh_one("rt-REUSED")
    assert result.kind == "superseded"
    assert result.error.startswith("superseded")


def test_refresh_one_invalid_grant_already_used_phrase_is_superseded():
    # Some deployments keep the generic code but say "already used" in text;
    # the canary must still fire so a rotation change is never missed.
    def fake_urlopen(req, timeout=None, context=None):
        raise _http_error(400, {"error": "invalid_grant",
                                 "error_description": "refresh token already used"})

    with patch("urllib.request.urlopen", fake_urlopen):
        result = crr.refresh_one("rt-X")
    assert result.kind == "superseded"


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


# ── classification helpers ───────────────────────────────────


def test_looks_superseded_matches_codes_and_phrases():
    assert crr._looks_superseded("refresh_token_reused", "") is True
    assert crr._looks_superseded("refresh_token_already_used", "") is True
    assert crr._looks_superseded("invalid_grant", "token already used") is True
    assert crr._looks_superseded("invalid_grant", "reused token") is True
    # ordinary revocation must NOT read as the canary
    assert crr._looks_superseded("invalid_grant", "token revoked") is False
    assert crr._looks_superseded("refresh_token_not_found", "") is False


# ── measured-state parsing ───────────────────────────────────


def test_parse_iso_ms_accepts_z_and_offset_rejects_junk():
    z = crr._parse_iso_ms("2026-08-14T00:00:00Z")
    off = crr._parse_iso_ms("2026-08-14T00:00:00+00:00")
    assert z == off and z is not None
    assert crr._parse_iso_ms(None) is None
    assert crr._parse_iso_ms("not-a-date") is None
    assert crr._parse_iso_ms("") is None


def test_failure_age_only_defined_while_erroring():
    now = crr._parse_iso_ms("2026-08-14T00:00:00Z") + _DAY_MS
    base = {"last_refresh_at": "2026-08-14T00:00:00Z"}
    # No error → no failure to age.
    assert crr._failure_age_ms(base, now_ms=now) is None
    # Erroring with a known last success → measured age.
    erroring = {**base, "last_refresh_error": "HTTP 429: blip"}
    assert crr._failure_age_ms(erroring, now_ms=now) == _DAY_MS
    # Erroring but never succeeded → age unknowable.
    never = {"last_refresh_error": "HTTP 429: blip"}
    assert crr._failure_age_ms(never, now_ms=now) is None


# ── _decide_refresh (the measured decision basis) ────────────


def test_decide_never_refreshed_refreshes():
    d = crr._decide_refresh({"refresh_token": "rt"}, now_ms=1_000)
    assert d.refresh is True
    assert d.basis == "never_refreshed"


def test_decide_recently_refreshed_skips():
    base = crr._parse_iso_ms("2026-08-14T00:00:00Z")
    payload = {"refresh_token": "rt", "last_refresh_at": "2026-08-14T00:00:00Z"}
    d = crr._decide_refresh(payload, now_ms=base + 1 * _HOUR_MS)
    assert d.refresh is False
    assert d.basis == "recently_refreshed"


def test_decide_stale_beyond_interval_refreshes():
    base = crr._parse_iso_ms("2026-08-14T00:00:00Z")
    payload = {"refresh_token": "rt", "last_refresh_at": "2026-08-14T00:00:00Z"}
    d = crr._decide_refresh(payload, now_ms=base + 13 * _HOUR_MS)
    assert d.refresh is True
    assert d.basis == "stale"


def test_decide_error_retries_every_tick_even_if_recent():
    # A transient error overrides the cadence: recover as fast as possible.
    base = crr._parse_iso_ms("2026-08-14T00:00:00Z")
    payload = {
        "refresh_token": "rt",
        "last_refresh_at": "2026-08-14T00:00:00Z",
        "last_refresh_error": "HTTP 429: blip",
    }
    d = crr._decide_refresh(payload, now_ms=base + 1 * _HOUR_MS)
    assert d.refresh is True
    assert d.basis == "retry_after_error"


def test_decide_revoked_and_superseded_skip():
    revoked = {"refresh_token": "rt", "last_refresh_error": "invalid_grant: gone"}
    superseded = {"refresh_token": "rt", "last_refresh_error": "superseded: reused"}
    assert crr._decide_refresh(revoked, now_ms=1).basis == "revoked"
    assert crr._decide_refresh(revoked, now_ms=1).refresh is False
    assert crr._decide_refresh(superseded, now_ms=1).basis == "superseded"
    assert crr._decide_refresh(superseded, now_ms=1).refresh is False


def test_decide_no_refresh_token_skips():
    d = crr._decide_refresh({"expires_at_ms": 0}, now_ms=1)
    assert d.refresh is False
    assert d.basis == "no_refresh_token"


def test_decide_ignores_expires_at_ms_entirely():
    # The whole point of the bead: expires_at_ms (a ~60-min clock) is NOT the
    # basis. A row whose id_token exp is a century out but which was last
    # refreshed 2 days ago is STALE and must refresh; a row expiring in a
    # second but refreshed a minute ago is fresh.
    base = crr._parse_iso_ms("2026-08-14T00:00:00Z")
    far_future = base + 100 * 365 * _DAY_MS
    stale = {
        "refresh_token": "rt",
        "expires_at_ms": far_future,
        "last_refresh_at": "2026-08-14T00:00:00Z",
    }
    assert crr._decide_refresh(stale, now_ms=base + 2 * _DAY_MS).refresh is True

    just_refreshed = {
        "refresh_token": "rt",
        "expires_at_ms": base + 1000,   # basically expired by the old logic
        "last_refresh_at": "2026-08-14T00:00:00Z",
    }
    d = crr._decide_refresh(just_refreshed, now_ms=base + 60_000)
    assert d.refresh is False
    assert d.basis == "recently_refreshed"


# ── refresh_credential_row ───────────────────────────────────


def _row(key: str, payload: dict):
    return SimpleNamespace(key=key, payload=payload)


def test_row_logs_decision_basis_every_tick(caplog):
    # Even a skipped row must log its measured basis (acceptance: log per tick).
    base = crr._parse_iso_ms("2026-08-14T00:00:00Z")
    row = _row("acct-1", {
        "refresh_token": "rt", "last_refresh_at": "2026-08-14T00:00:00Z",
    })
    with caplog.at_level(logging.INFO, logger=crr.logger.name):
        with patch.object(crr, "refresh_one",
                          side_effect=AssertionError("should skip")):
            out = crr.refresh_credential_row(
                row, org="personal", now_ms=base + _HOUR_MS, now_iso="now",
            )
    assert out is None
    line = "\n".join(caplog.messages)
    assert "decision=skip" in line
    assert "basis=recently_refreshed" in line
    assert "last_refresh_age_ms=" in line


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


def test_row_skips_recently_refreshed_row():
    base = crr._parse_iso_ms("2026-08-14T00:00:00Z")
    row = _row("acct-1", {
        "refresh_token": "rt",
        "last_refresh_at": "2026-08-14T00:00:00Z",
    })

    def boom(*a, **k):
        raise AssertionError("should not refresh a recently-refreshed row")

    with patch.object(crr, "refresh_one", boom):
        out = crr.refresh_credential_row(
            row, org="personal", now_ms=base + _HOUR_MS, now_iso="now",
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
        "expires_at_ms": now + 1000,
        "last_refresh_error": "HTTP 429: earlier blip",  # transient → retry
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


def test_row_superseded_stamps_canary_and_alarms(caplog):
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

    with caplog.at_level(logging.ERROR, logger=crr.logger.name):
        with patch.object(crr, "refresh_one",
                          return_value=crr.RefreshResult(
                              kind="superseded",
                              error="superseded: refresh_token_reused")):
            with patch.object(crr.graph_ops, "upsert_by_key", fake_upsert):
                out = crr.refresh_credential_row(
                    row, org="personal", now_ms=now, now_iso="now",
                )
    assert out.kind == "superseded"
    # tokens kept (this is not a mint), error stamped with the canary prefix
    p = captured["payload"]
    assert p["refresh_token"] == "rt-OLD"
    assert p["last_refresh_error"].startswith("superseded")
    # a named, distinct incident line — not folded into revocation
    joined = "\n".join(caplog.messages)
    assert "SUPERSEDED-TOKEN CANARY" in joined


# ── failure-age alarm ────────────────────────────────────────


def test_failure_age_alarm_fires_past_threshold(caplog):
    # A dead (revoked) row we skip is exactly the row that should alarm once
    # it has been failing longer than the threshold. No network is touched.
    base = crr._parse_iso_ms("2026-08-14T00:00:00Z")
    row = _row("acct-1", {
        "refresh_token": "rt",
        "last_refresh_at": "2026-08-14T00:00:00Z",
        "last_refresh_error": "invalid_grant: gone",
    })
    with caplog.at_level(logging.ERROR, logger=crr.logger.name):
        with patch.object(crr, "refresh_one",
                          side_effect=AssertionError("no network on skip")):
            out = crr.refresh_credential_row(
                row, org="personal",
                now_ms=base + 25 * _HOUR_MS, now_iso="now",
            )
    assert out is None
    joined = "\n".join(caplog.messages)
    assert "ALARM" in joined
    assert "25.0h" in joined


def test_failure_age_alarm_silent_below_threshold(caplog):
    base = crr._parse_iso_ms("2026-08-14T00:00:00Z")
    row = _row("acct-1", {
        "refresh_token": "rt",
        "last_refresh_at": "2026-08-14T00:00:00Z",
        "last_refresh_error": "invalid_grant: gone",
    })
    with caplog.at_level(logging.WARNING, logger=crr.logger.name):
        with patch.object(crr, "refresh_one",
                          side_effect=AssertionError("no network on skip")):
            crr.refresh_credential_row(
                row, org="personal",
                now_ms=base + 1 * _HOUR_MS, now_iso="now",
            )
    assert "ALARM" not in "\n".join(caplog.messages)


def test_failure_age_alarm_when_never_refreshed(caplog):
    # Erroring but no last_refresh_at: age is unknowable, still at risk.
    row = _row("acct-1", {
        "refresh_token": "rt",
        "last_refresh_error": "invalid_grant: gone",
    })
    with caplog.at_level(logging.WARNING, logger=crr.logger.name):
        with patch.object(crr, "refresh_one",
                          side_effect=AssertionError("no network on skip")):
            crr.refresh_credential_row(
                row, org="personal", now_ms=10, now_iso="now",
            )
    joined = "\n".join(caplog.messages)
    assert "ALARM" in joined
    assert "never refreshed" in joined


# ── refresh_all_credentials ──────────────────────────────────


def test_refresh_all_counts_outcomes():
    now_far = 4102444800 * 1000
    fresh_ts = crr._now_iso()  # ~now → recently refreshed → skipped
    rows = [
        _row("fresh", {"refresh_token": "rt", "last_refresh_at": fresh_ts}),
        _row("stale", {"refresh_token": "rt"}),  # never refreshed → refresh
        _row("revoked", {"refresh_token": "rt",
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
    assert counters["ok"] == 1        # the never-refreshed row refreshed
    assert counters["skipped"] == 2   # fresh + revoked skipped
    assert "superseded" in counters


def test_refresh_all_counts_superseded_outcome():
    rows = [_row("acct", {"refresh_token": "rt"})]  # never refreshed → refresh
    members = SimpleNamespace(members=rows)

    with patch.object(crr.graph_ops, "read_set",
                      return_value=members):
        with patch.object(crr.graph_ops, "upsert_by_key", lambda *a, **k: None):
            with patch.object(crr, "refresh_one",
                              return_value=crr.RefreshResult(
                                  kind="superseded",
                                  error="superseded: refresh_token_reused")):
                counters = crr.refresh_all_credentials()
    assert counters["superseded"] == 1


def test_refresh_all_read_set_failure_returns_zero_counters():
    def boom(*a, **k):
        raise RuntimeError("db down")

    with patch.object(crr.graph_ops, "read_set", boom):
        counters = crr.refresh_all_credentials()
    assert counters == {
        "ok": 0, "revoked": 0, "superseded": 0, "transient": 0, "skipped": 0,
    }


def test_refresh_all_empty_set_warns_with_remedy(caplog):
    # A Codex-enabled fleet with zero credential rows is the exact outage
    # signature — it must WARN (not INFO) and name the remedy inline.
    with caplog.at_level("INFO", logger=crr.logger.name):
        with patch.object(crr.graph_ops, "read_set",
                          return_value=SimpleNamespace(members=[])):
            counters = crr.refresh_all_credentials()
    assert counters == {
        "ok": 0, "revoked": 0, "superseded": 0, "transient": 0, "skipped": 0,
    }
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
    # Measured decision, not expires_at threshold: the never-refreshed row
    # (no last_refresh_at) refreshes; the row with no refresh_token is
    # skipped and — carrying no last_refresh_error — is not a standing
    # failure. Result: 1 refreshed, no standing failures.
    rows = [
        _row("new", {"refresh_token": "rt"}),
        _row("norefresh", {"expires_at_ms": now_far}),
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
    # A revoked (invalid_grant) row is skipped deterministically — no refresh
    # attempt, no network — yet still counts as a standing failure whose age
    # the tick log reports from last_refresh_at.
    failing = _row("bad", {
        "refresh_token": "rt", "expires_at_ms": now + 9 * 24 * 3600 * 1000,
        "last_refresh_error": "invalid_grant: refresh chain dead",
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
