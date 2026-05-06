"""OAuth/PKCE primitive tests for ``graph claude install`` (graph://73c4e9ef-bbc).

Mocks ``urllib.request.urlopen`` at the helper boundary so the assertions
exercise URL construction, request bodies, header construction, and
response parsing without spinning up a real OAuth provider.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import urllib.error
import urllib.parse
from unittest.mock import patch

import pytest

from tools.graph import claude_oauth
from tools.graph.claude_oauth import (
    AUTHORIZE_URL,
    CLIENT_ID,
    CONSOLE_SCOPES,
    CONSUMER_SCOPES,
    MINT_URL,
    OAuthError,
    TOKEN_URL,
    build_authorize_url,
    exchange_code_for_token,
    generate_pkce_pair,
    mint_setup_token,
    parse_token_response,
)


# ── PKCE ─────────────────────────────────────────────────────


def test_pkce_pair_yields_s256_challenge():
    verifier, challenge = generate_pkce_pair()
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).decode("ascii").rstrip("=")
    assert challenge == expected
    assert "=" not in challenge


def test_pkce_pair_uses_url_safe_chars_only():
    verifier, _ = generate_pkce_pair()
    # RFC 7636 verifier: 43..128 chars of A-Z / a-z / 0-9 / -._~
    assert 43 <= len(verifier) <= 128
    allowed = set(
        "abcdefghijklmnopqrstuvwxyz"
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "0123456789-._~"
    )
    assert set(verifier) <= allowed


def test_pkce_pair_is_random():
    a = generate_pkce_pair()
    b = generate_pkce_pair()
    assert a != b


# ── authorize URL ────────────────────────────────────────────


def test_build_authorize_url_consumer_scopes():
    url = build_authorize_url(
        scope=CONSUMER_SCOPES,
        code_challenge="abc-challenge",
        redirect_uri="http://localhost:5555/cb",
        state="state-123",
    )
    parsed = urllib.parse.urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == AUTHORIZE_URL
    params = urllib.parse.parse_qs(parsed.query)
    assert params["client_id"] == [CLIENT_ID]
    assert params["response_type"] == ["code"]
    assert params["scope"] == [CONSUMER_SCOPES]
    assert params["code_challenge"] == ["abc-challenge"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["redirect_uri"] == ["http://localhost:5555/cb"]
    assert params["state"] == ["state-123"]


def test_build_authorize_url_console_scopes():
    url = build_authorize_url(
        scope=CONSOLE_SCOPES,
        code_challenge="ch",
        redirect_uri="http://localhost:5555/cb",
    )
    params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert params["scope"] == [CONSOLE_SCOPES]
    assert "state" not in params


# ── token parsing ────────────────────────────────────────────


def _valid_token_body() -> dict:
    return {
        "access_token": "at-abc",
        "refresh_token": "rt-abc",
        "expires_in": 3600,
        "scope": "user:profile user:inference",
        "organization": {"uuid": "org-uuid-1", "name": "Org Name"},
        "account": {"uuid": "acct-uuid-1", "email_address": "user@example.com"},
    }


def test_parse_token_response_happy_path():
    parsed = parse_token_response(_valid_token_body())
    assert parsed.access_token == "at-abc"
    assert parsed.refresh_token == "rt-abc"
    assert parsed.expires_in == 3600
    assert parsed.organization_uuid == "org-uuid-1"
    assert parsed.organization_name == "Org Name"
    assert parsed.account_email == "user@example.com"


@pytest.mark.parametrize(
    "mutator",
    [
        lambda b: b.pop("access_token"),
        lambda b: b.pop("refresh_token"),
        lambda b: b.pop("expires_in"),
        lambda b: b.update({"organization": {}}),
        lambda b: b.update({"account": {}}),
    ],
)
def test_parse_token_response_rejects_missing_fields(mutator):
    body = _valid_token_body()
    mutator(body)
    with pytest.raises(OAuthError):
        parse_token_response(body)


# ── exchange_code_for_token ──────────────────────────────────


class _FakeResp:
    def __init__(self, payload: dict, status: int = 200):
        self._body = json.dumps(payload).encode()
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def test_exchange_code_for_token_posts_form_to_token_endpoint():
    captured: dict = {}

    def fake_urlopen(req, timeout=None, context=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["body"] = req.data.decode("ascii")
        captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
        return _FakeResp(_valid_token_body())

    with patch("urllib.request.urlopen", fake_urlopen):
        result = exchange_code_for_token(
            code="auth-code-123",
            code_verifier="verifier-xyz",
            redirect_uri="http://localhost:5555/cb",
        )

    assert captured["url"] == TOKEN_URL
    assert captured["method"] == "POST"
    body_params = urllib.parse.parse_qs(captured["body"])
    assert body_params["grant_type"] == ["authorization_code"]
    assert body_params["code"] == ["auth-code-123"]
    assert body_params["code_verifier"] == ["verifier-xyz"]
    assert body_params["client_id"] == [CLIENT_ID]
    assert body_params["redirect_uri"] == ["http://localhost:5555/cb"]
    assert captured["headers"]["content-type"] == "application/x-www-form-urlencoded"
    assert result.access_token == "at-abc"


def test_exchange_code_for_token_surfaces_http_error():
    err_body = json.dumps({"error": "invalid_grant", "error_description": "bad code"})

    def fake_urlopen(req, timeout=None, context=None):
        raise urllib.error.HTTPError(
            req.full_url, 400, "Bad Request", {},
            io.BytesIO(err_body.encode()),
        )

    with patch("urllib.request.urlopen", fake_urlopen):
        with pytest.raises(OAuthError) as exc:
            exchange_code_for_token(
                code="x", code_verifier="y", redirect_uri="http://localhost:5555/cb",
            )
    assert "400" in str(exc.value)
    assert "bad code" in str(exc.value)


# ── mint_setup_token ─────────────────────────────────────────


def test_mint_setup_token_posts_bearer_to_mint_endpoint():
    captured: dict = {}

    def fake_urlopen(req, timeout=None, context=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["body"] = req.data
        captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
        return _FakeResp({"raw_key": "sk-ant-oat01-XYZ"})

    with patch("urllib.request.urlopen", fake_urlopen):
        result = mint_setup_token(console_access_token="bearer-here")

    assert captured["url"] == MINT_URL
    assert captured["method"] == "POST"
    assert captured["body"] == b""
    assert captured["headers"]["authorization"] == "Bearer bearer-here"
    assert result == "sk-ant-oat01-XYZ"


def test_mint_setup_token_rejects_missing_raw_key():
    def fake_urlopen(req, timeout=None, context=None):
        return _FakeResp({"not_raw_key": "x"})

    with patch("urllib.request.urlopen", fake_urlopen):
        with pytest.raises(OAuthError):
            mint_setup_token(console_access_token="bearer")


def test_mint_setup_token_surfaces_http_error():
    err_body = json.dumps({"error": "insufficient_scope"})

    def fake_urlopen(req, timeout=None, context=None):
        raise urllib.error.HTTPError(
            req.full_url, 403, "Forbidden", {},
            io.BytesIO(err_body.encode()),
        )

    with patch("urllib.request.urlopen", fake_urlopen):
        with pytest.raises(OAuthError) as exc:
            mint_setup_token(console_access_token="bearer")
    assert "403" in str(exc.value)
