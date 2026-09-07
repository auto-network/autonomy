"""Stable voice ASGI host. Run without Uvicorn reload, outside dashboard workers."""
import asyncio
import os
import logging
import ssl
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute

from tools.dashboard import feature_flags, unlock_routes
from tools.dashboard.voice_transport import serve_voice
from tools.data_paths import DATA_ROOT


logger = logging.getLogger(__name__)


def valid_origin(origin: str, host: str) -> bool:
    """Compare public browser authority; forwarded headers confer no authority."""
    try:
        tls = os.environ.get("DASHBOARD_TLS") != "off"
        scheme = "https" if tls else "http"
        source = urlsplit(origin)
        target = urlsplit(scheme + "://" + host)
        # The dashboard may be reachable on more than one public port at once
        # (e.g. 443 via `tailscale serve` and 8080 published directly); accept a
        # comma-separated list. Live 2026-09-07: the operator's browser was on
        # :8080 and every socket was refused as an Origin mismatch.
        dashboard_ports = {
            int(p) for p in os.environ.get(
                "VOICE_DASHBOARD_PUBLIC_PORT", "443" if tls else "80"
            ).split(",") if p.strip()
        }
        voice_port = int(os.environ.get("VOICE_PUBLIC_PORT", "8443"))
        return bool(
            source.scheme == scheme and source.hostname and target.hostname
            and source.hostname.lower() == target.hostname.lower()
            and not source.username and not source.password
            and not target.username and not target.password
            and not source.path and not source.query and not source.fragment
            and not target.path and not target.query and not target.fragment
            and (source.port or (443 if tls else 80)) in dashboard_ports
            and target.port == voice_port
        )
    except (ValueError, TypeError):
        return False


def session_exists(bind: str) -> bool:
    # The persisted lifecycle table remains readable through dashboard downtime.
    # No Docker socket or host tmux socket is granted to the voice service.
    from tools.dashboard.dao.dashboard_db import get_session
    row = get_session(bind)
    return bool(row and row.get("state") not in {"ENDED", "FAILED"})


async def commit_text(bind: str, text: str) -> None:
    token_path = Path(os.environ.get("VOICE_SERVICE_TOKEN_FILE", str(DATA_ROOT / "voice-service.token")))
    token = token_path.read_text().strip()
    endpoint = os.environ.get("VOICE_DASHBOARD_URL", "https://dashboard:8080").rstrip("/")
    # Trust the same node certificate used for the public endpoint. The Compose
    # alias is internal, so verify the certificate chain without public DNS-name
    # matching rather than disabling certificate validation entirely.
    tls = ssl.create_default_context()
    if endpoint.startswith("https://"):
        tls.load_verify_locations(cafile=os.environ.get("AUTONOMY_TLS_CERT", str(DATA_ROOT / "tls.crt")))
    tls.check_hostname = False
    async with httpx.AsyncClient(verify=tls, timeout=10, trust_env=False) as client:
        response = await client.post(
            endpoint + "/api/internal/voice-commit",
            headers={"Authorization": "Bearer " + token},
            json={"bind": bind, "text": text},
        )
        response.raise_for_status()


async def voice_socket(websocket):
    # Name the refusal: uvicorn logs every pre-accept close as a bare 403, which
    # is indistinguishable between an Origin mismatch and a missing/invalid
    # login cookie (live 2026-09-07: "no voice at all" with only 403s to go on).
    origin = websocket.headers.get("origin", "")
    host = websocket.headers.get("host", "")
    if not valid_origin(origin, host):
        logger.warning("voice socket refused: origin mismatch origin=%r host=%r", origin, host)
        await websocket.close(code=4403)
        return
    cookie = websocket.cookies.get(unlock_routes.SESSION_COOKIE)
    identity = await asyncio.to_thread(unlock_routes.verify_session_token, cookie)
    if identity is None:
        logger.warning(
            "voice socket refused: %s login cookie (host=%r cookies=%s)",
            "invalid/expired/revoked" if cookie else "missing", host,
            sorted(websocket.cookies.keys()),
        )
        await websocket.close(code=4401)
        return
    bind_exists = await asyncio.to_thread(session_exists, websocket.query_params.get("bind", ""))
    feature_flags.invalidate_cache(all_orgs=True)
    # Signed login sid isolates concurrent operators even when they choose the
    # same delivery session. Never accept the buffer owner from query parameters.
    await serve_voice(
        websocket, session_exists=lambda bind: bind_exists, commit_text=commit_text,
        buffer_owner=identity["sid"],
    )


async def health(request):
    return JSONResponse({"ok": True, "service": "voice-gateway"})


app = Starlette(routes=[
    Route("/health", health),
    WebSocketRoute("/ws/voice", voice_socket),
])
