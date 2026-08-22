"""Minimal, non-durable Web Push proof for a real browser installation.

This module answers one structural question only: can this dashboard origin
subscribe a browser and send one standards-based encrypted Web Push message
that wakes the service worker?  It deliberately has no subscription table,
preferences, attention adapter, retry worker, or semantic notification state.

The proof endpoint accepts only Apple Web Push endpoints.  The production
sender will use the full DNS-pinned egress policy in graph://4145b11d-e70;
keeping this spike Apple-only avoids exposing an operator-authenticated SSRF
primitive while we prove iOS delivery.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import threading
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from tools.dashboard import api_auth
from tools.data_paths import resolve_store


logger = logging.getLogger(__name__)

_MAX_BODY_BYTES = 8 * 1024
_MAX_ENDPOINT_CHARS = 4096
_B64URL = re.compile(r"^[A-Za-z0-9_-]+$")
_VAPID_PATH = resolve_store("web_push_proof_vapid")
_vapid_lock = threading.Lock()
_vapid = None


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _decode_b64url(value: object, *, name: str, exact_len: int) -> bytes:
    if not isinstance(value, str) or not value or not _B64URL.fullmatch(value):
        raise ValueError(f"{name} must be unpadded base64url")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:
        raise ValueError(f"{name} is not valid base64url") from exc
    if len(decoded) != exact_len:
        raise ValueError(f"{name} must decode to {exact_len} bytes")
    return decoded


def _load_vapid():
    """Load one process-shared proof key, creating it mode-0600 if absent."""

    global _vapid
    with _vapid_lock:
        if _vapid is not None:
            return _vapid
        try:
            from py_vapid import Vapid
        except ImportError as exc:  # deployment not rebuilt with requirements
            raise RuntimeError(
                "Web Push proof runtime is unavailable; install "
                "deploy/requirements.txt and restart the dashboard"
            ) from exc

        _VAPID_PATH.parent.mkdir(parents=True, exist_ok=True)
        if _VAPID_PATH.exists():
            vapid = Vapid.from_file(private_key_file=str(_VAPID_PATH))
            # Repair permissive modes left by an interrupted/manual spike.
            os.chmod(_VAPID_PATH, 0o600)
        else:
            vapid = Vapid()
            vapid.generate_keys()
            pem = vapid.private_pem()
            try:
                fd = os.open(
                    _VAPID_PATH,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError:
                vapid = Vapid.from_file(private_key_file=str(_VAPID_PATH))
            else:
                with os.fdopen(fd, "wb") as key_file:
                    key_file.write(pem)
                os.chmod(_VAPID_PATH, 0o600)
        _vapid = vapid
        return vapid


def _application_server_key(vapid) -> str:
    raw = vapid.public_key.public_bytes(
        Encoding.X962,
        PublicFormat.UncompressedPoint,
    )
    return _b64url(raw)


def _validate_subscription(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("subscription must be an object")
    if set(value) - {"endpoint", "expirationTime", "keys"}:
        raise ValueError("subscription contains unsupported fields")

    endpoint = value.get("endpoint")
    if not isinstance(endpoint, str) or not endpoint \
            or len(endpoint) > _MAX_ENDPOINT_CHARS:
        raise ValueError("subscription endpoint is invalid")
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("subscription endpoint is invalid") from exc
    host = (parsed.hostname or "").rstrip(".").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password \
            or parsed.fragment or port not in (None, 443):
        raise ValueError("proof endpoint must be plain HTTPS on port 443")
    if not host.endswith(".push.apple.com"):
        raise ValueError("this iOS proof accepts only Apple Web Push endpoints")

    keys = value.get("keys")
    if not isinstance(keys, dict) or set(keys) != {"p256dh", "auth"}:
        raise ValueError("subscription keys must contain p256dh and auth")
    p256dh = _decode_b64url(keys.get("p256dh"), name="p256dh", exact_len=65)
    if p256dh[0] != 0x04:
        raise ValueError("p256dh must be an uncompressed P-256 point")
    _decode_b64url(keys.get("auth"), name="auth", exact_len=16)

    return {
        "endpoint": endpoint,
        "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]},
    }


def _same_origin_post(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return False
    try:
        supplied = urlsplit(origin)
        expected = urlsplit(str(request.base_url))
    except ValueError:
        return False
    return (
        supplied.scheme == expected.scheme
        and supplied.hostname == expected.hostname
        and supplied.port == expected.port
        and not supplied.username
        and not supplied.password
    )


def _send_push(subscription: dict, *, contact: str) -> int:
    try:
        import requests
        from pywebpush import webpush
    except ImportError as exc:
        raise RuntimeError(
            "Web Push proof runtime is unavailable; install "
            "deploy/requirements.txt and restart the dashboard"
        ) from exc

    class NoRedirectSession(requests.Session):
        def request(self, *args, **kwargs):
            kwargs["allow_redirects"] = False
            return super().request(*args, **kwargs)

    session = NoRedirectSession()
    session.trust_env = False
    payload = json.dumps({
        "v": 1,
        "title": "Autonomy Web Push works",
        "body": "An encrypted test reached this device.",
        "route": "/web-push-proof",
        "tag": "autonomy-web-push-proof",
    }, separators=(",", ":"))
    response = webpush(
        subscription_info=subscription,
        data=payload,
        vapid_private_key=_load_vapid(),
        vapid_claims={"sub": contact},
        content_encoding="aes128gcm",
        ttl=300,
        timeout=10,
        headers={"Urgency": "high", "Topic": "autonomy-push-proof"},
        requests_session=session,
    )
    return int(response.status_code)


async def service_worker(_request: Request) -> Response:
    script = Path(__file__).with_name("static").joinpath(
        "service-worker.js"
    ).read_text(encoding="utf-8")
    return Response(
        script,
        media_type="application/javascript",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Service-Worker-Allowed": "/",
        },
    )


async def api_config(request: Request) -> JSONResponse:
    refused = api_auth.require_global_api_authority(request)
    if refused is not None:
        return refused
    try:
        public_key = _application_server_key(_load_vapid())
    except (OSError, RuntimeError, ValueError) as exc:
        logger.warning("web_push_proof_config_unavailable error=%s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)
    return JSONResponse({
        "ok": True,
        "application_server_key": public_key,
        "durable": False,
        "apple_only": True,
    })


async def api_send(request: Request) -> JSONResponse:
    refused = api_auth.require_global_api_authority(request)
    if refused is not None:
        return refused
    if not _same_origin_post(request):
        return JSONResponse({"ok": False, "error": "same-origin request required"},
                            status_code=403)
    content_type = request.headers.get("content-type", "").split(";", 1)[0]
    if content_type.strip().lower() != "application/json":
        return JSONResponse({"ok": False, "error": "application/json required"},
                            status_code=415)
    raw = await request.body()
    if len(raw) > _MAX_BODY_BYTES:
        return JSONResponse({"ok": False, "error": "request is too large"},
                            status_code=413)
    try:
        body = json.loads(raw)
        if not isinstance(body, dict) or set(body) != {"subscription"}:
            raise ValueError("body must contain only subscription")
        subscription = _validate_subscription(body["subscription"])
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)

    # py-vapid currently requires the contact form to be mailto even though
    # RFC 8292 also permits an https URI. This non-routable address is scoped
    # to the structural proof; production must configure a real contact.
    contact = "mailto:webpush-proof@autonomy.invalid"
    try:
        status = await asyncio.to_thread(
            _send_push, subscription, contact=contact,
        )
    except Exception as exc:
        # The library's exception text may contain the endpoint or push-service
        # response body. Keep both out of the browser and ordinary logs.
        logger.warning(
            "web_push_proof_send_failed error_type=%s", type(exc).__name__,
        )
        return JSONResponse({
            "ok": False,
            "error": "the push service did not accept the proof message",
            "error_type": type(exc).__name__,
        }, status_code=502)
    return JSONResponse({
        "ok": True,
        "push_service_status": status,
        "meaning": "accepted by push service; waiting for device display",
    })
