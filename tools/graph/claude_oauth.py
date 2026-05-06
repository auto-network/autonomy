"""Claude OAuth + PKCE flow primitives for ``graph claude install``.

Implements the local-loopback authorization-code-with-PKCE flow that the
Claude CLI itself uses for both consumer (login) and console
(setup-token mint) scope sets. Constants extracted from Claude binary
2.1.128.

The shape is split into pure helpers (``build_authorize_url``,
``exchange_code_for_token``, ``mint_setup_token``) plus a stateful
:class:`OAuthFlow` that owns the loopback HTTP server and orchestrates
the round-trip. Tests pin the helpers to mock endpoints; ``OAuthFlow``
itself runs only against the real dashboard or a recorded fixture.

Spec: graph://73c4e9ef-bbc.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import logging
import os
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from typing import Any, Callable


logger = logging.getLogger(__name__)


# Constants from Claude CLI binary 2.1.128.
AUTHORIZE_URL = "https://platform.claude.com/oauth/authorize"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
MINT_URL = "https://api.anthropic.com/api/oauth/claude_cli/create_api_key"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"

CONSUMER_SCOPES = (
    "user:profile user:inference user:sessions:claude_code "
    "user:mcp_servers user:file_upload"
)
CONSOLE_SCOPES = "org:create_api_key user:profile"


class OAuthError(RuntimeError):
    """Raised when the OAuth flow fails (network, validation, or operator)."""


# ── PKCE helpers ─────────────────────────────────────────────


def generate_pkce_pair() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` for an S256 PKCE pair.

    ``code_verifier`` is 43..128 chars of unreserved URL-safe characters
    (RFC 7636 § 4.1). We use 32 random bytes → 43-char base64url string.
    The challenge is ``BASE64URL(SHA256(verifier))``, stripped of padding.
    """
    verifier_bytes = secrets.token_urlsafe(32)
    # token_urlsafe already returns base64url without padding; size is
    # ~43 chars for 32 bytes. RFC 7636 calls for 43..128 chars verbatim.
    verifier = verifier_bytes
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


# ── HTTP helpers ─────────────────────────────────────────────


def _post_form(url: str, data: dict[str, str]) -> dict[str, Any]:
    """POST form-encoded ``data`` to ``url`` and return the JSON body.

    Surfaces non-2xx with a clean :class:`OAuthError`. Used for the
    authorization-code → token exchange. We rely on urllib (stdlib) so
    the OAuth path works in any environment without optional deps.
    """
    encoded = urllib.parse.urlencode(data).encode("ascii")
    req = urllib.request.Request(
        url,
        data=encoded,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        body = e.read() or b""
        try:
            parsed = json.loads(body.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            parsed = {}
        raise OAuthError(
            f"token endpoint returned HTTP {e.code}: "
            f"{parsed.get('error_description') or parsed.get('error') or body!r}"
        ) from None
    except urllib.error.URLError as e:
        raise OAuthError(f"token endpoint unreachable: {e.reason}") from None
    try:
        return json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise OAuthError(f"token endpoint returned non-JSON body: {e}") from None


def _post_bearer(url: str, bearer_token: str) -> dict[str, Any]:
    """POST an empty body to ``url`` with ``Authorization: Bearer <token>``."""
    req = urllib.request.Request(
        url,
        data=b"",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {bearer_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        body = e.read() or b""
        try:
            parsed = json.loads(body.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            parsed = {}
        raise OAuthError(
            f"mint endpoint returned HTTP {e.code}: "
            f"{parsed.get('error_description') or parsed.get('error') or body!r}"
        ) from None
    except urllib.error.URLError as e:
        raise OAuthError(f"mint endpoint unreachable: {e.reason}") from None
    try:
        return json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise OAuthError(f"mint endpoint returned non-JSON body: {e}") from None


# ── Authorize URL builder ────────────────────────────────────


def build_authorize_url(
    *,
    scope: str,
    code_challenge: str,
    redirect_uri: str,
    state: str | None = None,
) -> str:
    """Compose the operator-facing authorize URL with PKCE params."""
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "scope": scope,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "redirect_uri": redirect_uri,
    }
    if state:
        params["state"] = state
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


# ── Token exchange + mint ────────────────────────────────────


@dataclass
class TokenResponse:
    """Parsed response from the OAuth ``v1/oauth/token`` endpoint."""

    access_token: str
    refresh_token: str
    expires_in: int
    scope: str
    organization_uuid: str
    organization_name: str
    account_uuid: str
    account_email: str
    raw: dict[str, Any]


def parse_token_response(body: dict[str, Any]) -> TokenResponse:
    """Validate + parse a token-endpoint JSON body into a :class:`TokenResponse`.

    Anthropic's response carries nested ``organization`` and ``account``
    objects. Missing fields raise :class:`OAuthError` so the install
    flow surfaces a clean message instead of a ``KeyError``.
    """
    if not isinstance(body, dict):
        raise OAuthError(f"token response is not a JSON object: {type(body).__name__}")
    access_token = body.get("access_token")
    refresh_token = body.get("refresh_token")
    expires_in = body.get("expires_in")
    scope = body.get("scope") or ""
    org = body.get("organization") or {}
    acct = body.get("account") or {}
    if not isinstance(access_token, str) or not access_token:
        raise OAuthError("token response missing 'access_token'")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise OAuthError("token response missing 'refresh_token'")
    if not isinstance(expires_in, int) or expires_in <= 0:
        raise OAuthError("token response missing/invalid 'expires_in'")
    if not isinstance(org, dict) or not org.get("uuid"):
        raise OAuthError("token response missing 'organization.uuid'")
    if not isinstance(acct, dict) or not acct.get("uuid"):
        raise OAuthError("token response missing 'account.uuid'")
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=int(expires_in),
        scope=scope,
        organization_uuid=str(org.get("uuid")),
        organization_name=str(org.get("name") or ""),
        account_uuid=str(acct.get("uuid")),
        account_email=str(acct.get("email_address") or ""),
        raw=body,
    )


def exchange_code_for_token(
    *,
    code: str,
    code_verifier: str,
    redirect_uri: str,
) -> TokenResponse:
    """Exchange an authorization code for an access/refresh token bundle."""
    body = _post_form(TOKEN_URL, {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": code_verifier,
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri,
    })
    return parse_token_response(body)


def mint_setup_token(*, console_access_token: str) -> str:
    """Mint a long-lived ``sk-ant-oat01-…`` setup token from a console bundle.

    Anthropic's response carries ``raw_key`` (and possibly other
    metadata; we only persist the key per the design).
    """
    body = _post_bearer(MINT_URL, console_access_token)
    raw_key = body.get("raw_key")
    if not isinstance(raw_key, str) or not raw_key:
        raise OAuthError("mint response missing 'raw_key'")
    return raw_key


# ── Loopback server ──────────────────────────────────────────


def pick_ephemeral_port() -> int:
    """Bind-and-release on port 0 to discover a free local port.

    The local listener binds to this port a moment later; in the
    interim a different process could grab it, but the window is small
    and the failure mode is "operator retries". Worth the simplicity.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


class _CodeCaptureHandler(http.server.BaseHTTPRequestHandler):
    """Single-shot handler that captures ``?code=...`` from the redirect."""

    # Filled in via attribute injection by :class:`OAuthFlow`.
    captured: dict[str, str] = {}
    expected_path: str = "/cb"

    def do_GET(self) -> None:  # noqa: N802 — http.server protocol name
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != self.expected_path:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"not found")
            return
        params = urllib.parse.parse_qs(parsed.query)
        code = params.get("code", [None])[0]
        state = params.get("state", [None])[0]
        error = params.get("error", [None])[0]
        if error:
            self.captured["error"] = error
            self.captured["error_description"] = (
                params.get("error_description", [""])[0] or ""
            )
        elif code:
            self.captured["code"] = code
            if state:
                self.captured["state"] = state
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        if "code" in self.captured:
            self.wfile.write(
                b"<!doctype html><html><body><h2>Login captured.</h2>"
                b"<p>You can close this tab.</p></body></html>"
            )
        else:
            err = self.captured.get("error", "unknown")
            self.wfile.write(
                f"<!doctype html><html><body><h2>Login failed.</h2>"
                f"<p>{err}</p></body></html>".encode("utf-8")
            )

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Silence the default stderr logging — we don't want the flow
        # printing one-line access logs over the operator's terminal.
        return


@dataclass
class AuthCodeCapture:
    code: str
    state: str | None


def run_loopback_capture(
    *,
    port: int,
    open_browser: Callable[[str], bool],
    authorize_url: str,
    timeout_seconds: float = 300.0,
) -> AuthCodeCapture:
    """Spin up a single-shot HTTP listener; open the browser; return the code.

    The server runs on a background thread so the main thread can poll
    for the captured code and time out cleanly. Raises
    :class:`OAuthError` on timeout or when the operator declined the
    consent screen (``?error=...``).

    ``open_browser`` is injected so tests can short-circuit the call —
    the real CLI uses :func:`webbrowser.open`.
    """
    handler_cls = type(
        "_CaptureHandler",
        (_CodeCaptureHandler,),
        {"captured": {}, "expected_path": "/cb"},
    )
    server = http.server.HTTPServer(("127.0.0.1", port), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        opened = False
        try:
            opened = bool(open_browser(authorize_url))
        except Exception as e:  # noqa: BLE001 — webbrowser is finicky
            logger.warning("webbrowser.open failed: %s", e)
        if not opened:
            print(
                "Could not open the browser automatically. Visit this URL:\n"
                f"  {authorize_url}",
            )
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if "code" in handler_cls.captured:
                return AuthCodeCapture(
                    code=handler_cls.captured["code"],
                    state=handler_cls.captured.get("state"),
                )
            if "error" in handler_cls.captured:
                err = handler_cls.captured["error"]
                desc = handler_cls.captured.get("error_description", "")
                raise OAuthError(
                    f"login failed: {err}"
                    + (f" — {desc}" if desc else "")
                )
            time.sleep(0.1)
        raise OAuthError(
            f"timed out after {int(timeout_seconds)}s waiting for the "
            "browser callback"
        )
    finally:
        server.shutdown()
        server.server_close()


# ── Flow orchestrator ────────────────────────────────────────


@dataclass
class FlowResult:
    """End-to-end result of one PKCE flow round-trip."""

    token: TokenResponse
    redirect_uri: str


def run_oauth_flow(
    *,
    scope: str,
    open_browser: Callable[[str], bool] | None = None,
    timeout_seconds: float = 300.0,
) -> FlowResult:
    """Drive one end-to-end PKCE round-trip for ``scope``.

    Generates a PKCE pair, picks an ephemeral port, opens the browser,
    captures the code on a loopback listener, exchanges for a token
    bundle. Returns the parsed :class:`TokenResponse` plus the
    ``redirect_uri`` actually used (callers must echo this back to
    the token endpoint, and we make the round-trip atomic by binding
    them together).
    """
    if open_browser is None:
        open_browser = webbrowser.open
    code_verifier, code_challenge = generate_pkce_pair()
    port = pick_ephemeral_port()
    redirect_uri = f"http://localhost:{port}/cb"
    state = secrets.token_urlsafe(16)
    authorize_url = build_authorize_url(
        scope=scope,
        code_challenge=code_challenge,
        redirect_uri=redirect_uri,
        state=state,
    )
    capture = run_loopback_capture(
        port=port,
        open_browser=open_browser,
        authorize_url=authorize_url,
        timeout_seconds=timeout_seconds,
    )
    if capture.state is not None and capture.state != state:
        raise OAuthError(
            "OAuth state mismatch — possible CSRF; aborting"
        )
    token = exchange_code_for_token(
        code=capture.code,
        code_verifier=code_verifier,
        redirect_uri=redirect_uri,
    )
    return FlowResult(token=token, redirect_uri=redirect_uri)
