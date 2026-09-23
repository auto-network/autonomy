"""Codex (ChatGPT-backed) OAuth refresh primitives.

Codex authenticates with a ChatGPT OAuth bundle — a short-lived
``access_token`` + ``id_token`` (both ~10-day JWTs) plus a long-lived,
opaque ``refresh_token``. The CLI normally self-refreshes on launch by
POSTing the refresh_token to ``auth.openai.com`` and rewriting
``~/.codex/auth.json`` in place. Inside our containers that file is
mounted read-only, so the in-container refresh can never land — the
substrate refresh poller (``tools/dashboard/codex_credentials_refresh``)
performs the refresh host-side instead and keeps the
``dashboard.codex.credentials`` row ahead of expiry.

This module is the pure OAuth surface that poller uses: the token
endpoint constant, the client_id, the refresh scope, and the JWT
claim/expiry helpers. It performs no substrate I/O.

Constants proven headless on 2026-05-31 (session ``df69566d-979``):
a ``grant_type=refresh_token`` POST with ``client_id``
``app_EMoamEEZ73f0CkXaXp7hrann`` and a scope including ``offline_access``
returns a rotated access/id/refresh triple with a clean HTTP 200. The
``offline_access`` scope is load-bearing — without it the endpoint mints
a new access_token but does **not** return a fresh refresh_token, so the
chain would decay to expiry.

Spec: bead auto-l1h3f (Codex credential end-state, STEP 2). Parity
reference: ``tools/graph/claude_oauth.py`` (``graph://73c4e9ef-bbc``).
"""

from __future__ import annotations

import base64
import json
from typing import Any


# ── Constants (from the codex CLI / auth.json id_token claims) ───────────

# The ChatGPT OAuth token endpoint. Accepts the refresh-token grant as
# form-encoded body (the shape the Codex CLI's own refresh uses).
CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"

# Public OAuth client_id baked into the Codex CLI. Carried in the
# access_token's ``client_id`` claim; identical across installs.
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

# Scope requested on refresh. MUST include ``offline_access`` so the
# endpoint rotates the refresh_token back to us — otherwise the refresh
# chain silently dies when the current refresh_token eventually expires.
CODEX_REFRESH_SCOPE = "openid profile email offline_access"

# A neutral User-Agent. The auth.openai.com token endpoint accepted the
# proven headless refresh without any special UA; we set a stable one so
# the request is attributable in traces and never advertises the harness.
CODEX_USER_AGENT = "autonomy-codex-refresh/1"


# ── JWT helpers ──────────────────────────────────────────────────────────


def decode_jwt_claims(token: str) -> dict[str, Any]:
    """Best-effort decode of a JWT payload segment (no signature check).

    Codex's ``id_token`` is a standard JWT; we only read its ``exp`` /
    identity claims, never trust it for authorization. Returns ``{}`` on
    any structural problem. Mirrors
    ``credential_import._decode_jwt_claims`` so the poller derives expiry
    exactly the way the import derived it.
    """
    if not isinstance(token, str):
        return {}
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    segment = parts[1]
    padding = "=" * (-len(segment) % 4)
    try:
        decoded = base64.urlsafe_b64decode(segment + padding)
        claims = json.loads(decoded)
    except (ValueError, json.JSONDecodeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def id_token_exp_ms(id_token: str) -> int | None:
    """Return the id_token's ``exp`` claim in epoch milliseconds, or ``None``.

    The substrate stores ``expires_at_ms`` as ``exp * 1000`` (see
    ``credential_import.build_codex_payload``); the refresh poller
    re-derives it identically from the freshly-minted id_token so a
    re-import and a poll converge on the same value.
    """
    claims = decode_jwt_claims(id_token)
    exp = claims.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, int):
        return None
    return exp * 1000
