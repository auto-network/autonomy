"""L3 browser check: Secure dashboard cookies round-trip on HTTP localhost."""

from __future__ import annotations

import shutil
import socket
import subprocess
import threading
import time

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from tools.dashboard import unlock_routes


async def _set_cookie(request: Request):
    response = PlainTextResponse("set")
    unlock_routes.attach_session_cookie(response, request, "browser-proof")
    return response


async def _read_cookie(request: Request):
    return PlainTextResponse(request.cookies.get(unlock_routes.SESSION_COOKIE) or "missing")


APP = Starlette(routes=[
    Route("/set", _set_cookie),
    Route("/read", _read_cookie),
])


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.skipif(shutil.which("agent-browser") is None,
                    reason="agent-browser not available")
def test_secure_cookie_round_trips_on_http_localhost_in_chromium():
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(
        APP, host="127.0.0.1", port=port, log_level="error",
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 5
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    assert server.started

    session = f"secure-cookie-{port}"
    command = ["agent-browser", "--session", session]
    try:
        opened = subprocess.run(
            [*command, "open", f"http://localhost:{port}/set"],
            capture_output=True, text=True, timeout=10,
        )
        assert opened.returncode == 0, opened.stderr
        read = subprocess.run(
            [*command, "open", f"http://localhost:{port}/read"],
            capture_output=True, text=True, timeout=10,
        )
        assert read.returncode == 0, read.stderr
        body = subprocess.run(
            [*command, "get", "text", "body"],
            capture_output=True, text=True, timeout=10,
        )
        assert body.returncode == 0, body.stderr
        assert body.stdout.strip() == "browser-proof"
    finally:
        subprocess.run([*command, "close"], capture_output=True, timeout=5)
        server.should_exit = True
        thread.join(timeout=5)
