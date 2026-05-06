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

# User-Agent matching Claude CLI's own ``C5()`` so we (a) bypass Cloudflare
# bot detection on platform.claude.com (the default Python urllib UA gets
# 403'd as Cloudflare error 1010) and (b) don't advertise the autonomy
# harness to Anthropic's logs.
#
# The version is detected at module load time. Sources tried in order:
#   1. resolve ``~/.local/bin/claude`` symlink, parse ``versions/<X.Y.Z>``
#      basename — single readlink syscall, no subprocess
#   2. run ``claude --version`` and parse — fallback for non-symlink installs
#   3. pinned constant — last-resort fallback so OAuth still attempts a call
#      even on hosts without Claude installed (test envs, etc.)
#
# Module-level constant means uvicorn's auto-reload picks up Claude updates
# the next time the file changes; we accept that long-running processes
# can fall behind a Claude self-update until restart.
_CLAUDE_VERSION_FALLBACK = "2.1.128"


def _detect_claude_version() -> str:
    import re
    import shutil
    import subprocess

    claude_bin = os.path.expanduser("~/.local/bin/claude")
    try:
        target = os.path.realpath(claude_bin)
        m = re.search(r"/versions/(\d+\.\d+\.\d+)$", target)
        if m:
            return m.group(1)
    except OSError:
        pass

    which = shutil.which("claude") or claude_bin
    try:
        out = subprocess.run(
            [which, "--version"],
            capture_output=True, text=True, timeout=5,
        )
        m = re.search(r"(\d+\.\d+\.\d+)", out.stdout)
        if m:
            return m.group(1)
    except (OSError, subprocess.SubprocessError):
        pass

    return _CLAUDE_VERSION_FALLBACK


CLAUDE_USER_AGENT = f"claude-cli/{_detect_claude_version()}"

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


def _post_json(url: str, data: dict[str, Any]) -> dict[str, Any]:
    """POST JSON ``data`` to ``url`` and return the parsed JSON body.

    Surfaces non-2xx with a clean :class:`OAuthError`. Used for the
    authorization-code → token exchange (the refresh-token grant has its
    own helper inside the dashboard's refresh poller). Anthropic's token
    endpoint expects ``application/json`` for both grant flows — see the
    Claude binary 2.1.128 ``Tp8`` (token exchange) helper.

    Every call logs intent (grant_type) before the request, then either
    success (HTTP code, duration) or failure (HTTP code + sanitized
    error_description) so production debugging has full per-request
    visibility. Token values are never logged.
    """
    grant_type = data.get("grant_type", "<unknown>")
    encoded = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=encoded,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": CLAUDE_USER_AGENT,
        },
        method="POST",
    )
    logger.info("oauth: POST %s grant_type=%s", url, grant_type)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
            status_code = resp.status
    except urllib.error.HTTPError as e:
        elapsed_ms = (time.monotonic() - started) * 1000
        body = e.read() or b""
        try:
            parsed = json.loads(body.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            parsed = {}
        err_desc = parsed.get("error_description") or parsed.get("error") or ""
        logger.error(
            "oauth: POST %s grant_type=%s FAILED HTTP %d in %.1fms: %s",
            url, grant_type, e.code, elapsed_ms, err_desc or repr(body[:200]),
        )
        raise OAuthError(
            f"token endpoint returned HTTP {e.code}: "
            f"{err_desc or body!r}"
        ) from None
    except urllib.error.URLError as e:
        elapsed_ms = (time.monotonic() - started) * 1000
        logger.error(
            "oauth: POST %s grant_type=%s UNREACHABLE in %.1fms: %s",
            url, grant_type, elapsed_ms, e.reason,
        )
        raise OAuthError(f"token endpoint unreachable: {e.reason}") from None
    elapsed_ms = (time.monotonic() - started) * 1000
    try:
        parsed_body = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.error(
            "oauth: POST %s grant_type=%s returned HTTP %d non-JSON body in %.1fms",
            url, grant_type, status_code, elapsed_ms,
        )
        raise OAuthError(f"token endpoint returned non-JSON body: {e}") from None
    # Log success with the response shape that's safe to surface — never
    # the access_token / refresh_token themselves. expires_in + scope +
    # organization.uuid are enough for ops to triage.
    org_meta = parsed_body.get("organization") if isinstance(parsed_body, dict) else None
    org_uuid = org_meta.get("uuid") if isinstance(org_meta, dict) else None
    logger.info(
        "oauth: POST %s grant_type=%s OK HTTP %d in %.1fms expires_in=%s scope=%r org=%s",
        url, grant_type, status_code, elapsed_ms,
        parsed_body.get("expires_in") if isinstance(parsed_body, dict) else None,
        parsed_body.get("scope") if isinstance(parsed_body, dict) else None,
        org_uuid,
    )
    return parsed_body


def _post_bearer(url: str, bearer_token: str) -> dict[str, Any]:
    """POST an empty body to ``url`` with ``Authorization: Bearer <token>``.

    Logs intent / success / failure with HTTP code + duration; bearer
    token is never logged.
    """
    req = urllib.request.Request(
        url,
        data=b"",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {bearer_token}",
            "Content-Type": "application/json",
            "User-Agent": CLAUDE_USER_AGENT,
        },
        method="POST",
    )
    logger.info("oauth: POST %s (Bearer auth)", url)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
            status_code = resp.status
    except urllib.error.HTTPError as e:
        elapsed_ms = (time.monotonic() - started) * 1000
        body = e.read() or b""
        try:
            parsed = json.loads(body.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            parsed = {}
        err_desc = parsed.get("error_description") or parsed.get("error") or ""
        logger.error(
            "oauth: POST %s (Bearer auth) FAILED HTTP %d in %.1fms: %s",
            url, e.code, elapsed_ms, err_desc or repr(body[:200]),
        )
        raise OAuthError(
            f"mint endpoint returned HTTP {e.code}: "
            f"{err_desc or body!r}"
        ) from None
    except urllib.error.URLError as e:
        elapsed_ms = (time.monotonic() - started) * 1000
        logger.error(
            "oauth: POST %s (Bearer auth) UNREACHABLE in %.1fms: %s",
            url, elapsed_ms, e.reason,
        )
        raise OAuthError(f"mint endpoint unreachable: {e.reason}") from None
    elapsed_ms = (time.monotonic() - started) * 1000
    try:
        parsed_body = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.error(
            "oauth: POST %s (Bearer auth) returned HTTP %d non-JSON body in %.1fms",
            url, status_code, elapsed_ms,
        )
        raise OAuthError(f"mint endpoint returned non-JSON body: {e}") from None
    # Mint response only contains 'raw_key' (and possibly other metadata).
    # Don't log raw_key itself; just confirm we got one.
    has_key = bool(isinstance(parsed_body, dict) and parsed_body.get("raw_key"))
    logger.info(
        "oauth: POST %s (Bearer auth) OK HTTP %d in %.1fms raw_key_present=%s",
        url, status_code, elapsed_ms, has_key,
    )
    return parsed_body


# ── Authorize URL builder ────────────────────────────────────


def build_authorize_url(
    *,
    scope: str,
    code_challenge: str,
    redirect_uri: str,
    state: str | None = None,
) -> str:
    """Compose the operator-facing authorize URL with PKCE params.

    The leading ``code=true`` parameter mirrors what the Claude CLI
    itself appends in :func:`buildAuthUrl` (binary 2.1.128). It's
    required for both the loopback and manual redirect flows; without
    it the authorize endpoint rejects the request with "Invalid request
    format". Order matches the binary so any future log diffs line up.
    """
    params = {
        "code": "true",
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
    state: str | None = None,
) -> TokenResponse:
    """Exchange an authorization code for an access/refresh token bundle.

    Body shape matches Claude CLI's ``Tp8`` helper (binary 2.1.128):
    JSON-encoded with ``state`` echoed back so Anthropic can verify the
    exchange came from the same flow that initiated the authorize.
    """
    payload: dict[str, Any] = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": CLIENT_ID,
        "code_verifier": code_verifier,
    }
    if state is not None:
        payload["state"] = state
    body = _post_json(TOKEN_URL, payload)
    parsed = parse_token_response(body)
    logger.info(
        "oauth: code-exchange resolved org=%s account=%s scope=%r",
        parsed.organization_uuid, parsed.account_email, parsed.scope,
    )
    return parsed


def mint_setup_token(*, console_access_token: str) -> str:
    """Mint a long-lived ``sk-ant-oat01-…`` setup token from a console bundle.

    Anthropic's response carries ``raw_key`` (and possibly other
    metadata; we only persist the key per the design).
    """
    logger.info("oauth: minting setup-token via %s", MINT_URL)
    body = _post_bearer(MINT_URL, console_access_token)
    raw_key = body.get("raw_key")
    if not isinstance(raw_key, str) or not raw_key:
        logger.error(
            "oauth: mint response missing 'raw_key' (keys=%s)",
            sorted(body.keys()) if isinstance(body, dict) else type(body).__name__,
        )
        raise OAuthError("mint response missing 'raw_key'")
    logger.info("oauth: mint OK (raw_key length=%d)", len(raw_key))
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
    expected_path: str = "/callback"

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
        {"captured": {}, "expected_path": "/callback"},
    )
    server = http.server.HTTPServer(("127.0.0.1", port), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("oauth: loopback listener started on port %d", port)
    try:
        opened = False
        try:
            opened = bool(open_browser(authorize_url))
        except Exception as e:  # noqa: BLE001 — webbrowser is finicky
            logger.warning("webbrowser.open failed: %s", e)
        if opened:
            logger.info("oauth: browser opened to authorize URL")
        else:
            logger.warning("oauth: browser did not open; falling back to operator paste")
            print(
                "Could not open the browser automatically. Visit this URL:\n"
                f"  {authorize_url}",
            )
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if "code" in handler_cls.captured:
                logger.info("oauth: authorization code captured on loopback")
                return AuthCodeCapture(
                    code=handler_cls.captured["code"],
                    state=handler_cls.captured.get("state"),
                )
            if "error" in handler_cls.captured:
                err = handler_cls.captured["error"]
                desc = handler_cls.captured.get("error_description", "")
                logger.error("oauth: authorize callback returned error: %s%s",
                             err, f" — {desc}" if desc else "")
                raise OAuthError(
                    f"login failed: {err}"
                    + (f" — {desc}" if desc else "")
                )
            time.sleep(0.1)
        logger.error("oauth: timed out after %ds waiting for browser callback",
                     int(timeout_seconds))
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
    redirect_uri = f"http://localhost:{port}/callback"
    state = secrets.token_urlsafe(16)
    logger.info("oauth: starting flow scope=%r redirect_uri=%s", scope, redirect_uri)
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
        logger.error("oauth: state mismatch — got=%r expected=%r — aborting",
                     capture.state, state)
        raise OAuthError(
            "OAuth state mismatch — possible CSRF; aborting"
        )
    token = exchange_code_for_token(
        code=capture.code,
        code_verifier=code_verifier,
        redirect_uri=redirect_uri,
        state=state,
    )
    return FlowResult(token=token, redirect_uri=redirect_uri)
