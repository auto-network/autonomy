"""Tests for the Codex OAuth primitives (bead auto-l1h3f)."""

from __future__ import annotations

import base64
import json

from tools.graph import codex_oauth as co


def _jwt(claims: dict) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(
        json.dumps(claims).encode("utf-8")
    ).decode().rstrip("=")
    return f"{header}.{payload}.sig"


def test_constants_are_the_proven_recipe():
    assert co.CODEX_TOKEN_URL == "https://auth.openai.com/oauth/token"
    assert co.CODEX_CLIENT_ID == "app_EMoamEEZ73f0CkXaXp7hrann"
    # offline_access is load-bearing: without it no rotated refresh_token.
    assert "offline_access" in co.CODEX_REFRESH_SCOPE


def test_decode_jwt_claims_reads_payload():
    tok = _jwt({"exp": 1234567890, "email": "a@b.com"})
    claims = co.decode_jwt_claims(tok)
    assert claims["exp"] == 1234567890
    assert claims["email"] == "a@b.com"


def test_decode_jwt_claims_bad_input_returns_empty():
    assert co.decode_jwt_claims("not-a-jwt") == {}
    assert co.decode_jwt_claims("") == {}
    assert co.decode_jwt_claims("a.b") == {}  # non-base64 payload → {}


def test_id_token_exp_ms_multiplies_to_millis():
    assert co.id_token_exp_ms(_jwt({"exp": 1_700_000_000})) == 1_700_000_000_000


def test_id_token_exp_ms_missing_or_nonint_exp_is_none():
    assert co.id_token_exp_ms(_jwt({})) is None
    assert co.id_token_exp_ms(_jwt({"exp": "soon"})) is None
    # booleans are not accepted as ints
    assert co.id_token_exp_ms(_jwt({"exp": True})) is None
    assert co.id_token_exp_ms("garbage") is None
