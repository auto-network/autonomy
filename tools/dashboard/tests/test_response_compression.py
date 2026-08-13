"""Responses are compressed, and the live-update stream is not.

Nothing the dashboard served was compressed. It mattered most for a mission
screen, which is one self-contained document -- state, platform runtime and the
author's markup all inline, because over a share link there is no origin to
fetch a second file from -- and which is deliberately ``no-store``. Every open
transferred the whole thing again, uncompressed: seconds of white screen on a
phone before anything drew.

The hazard in fixing that is the live-update stream. Compressing an open
``text/event-stream`` buffers events behind the compressor, which would break
the thing that makes a mission page update while it is being read. Starlette
excludes that media type by default -- these pin it, because the exclusion is
load-bearing and lives in a dependency.
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import HTMLResponse, StreamingResponse
from starlette.routing import Route
from starlette.testclient import TestClient

# Comfortably over minimum_size, and representative: a real mission document is
# ~180KB of highly compressible markup.
BIG = "<p>a mission screen, mostly markup</p>" * 400


def _app() -> Starlette:
    async def page(request):
        return HTMLResponse(BIG)

    async def stream(request):
        async def gen():
            yield b"event: activity\ndata: {}\n\n"

        # Exactly what sse_starlette sets, charset parameter included -- the
        # matcher has to strip that before comparing or the exclusion silently
        # misses and the stream gets compressed.
        return StreamingResponse(gen(), media_type="text/event-stream; charset=utf-8")

    return Starlette(
        routes=[Route("/page", page), Route("/events", stream)],
        middleware=[Middleware(GZipMiddleware, minimum_size=1024)],
    )


def test_a_page_is_compressed_when_the_client_offers_it():
    with TestClient(_app()) as client:
        r = client.get("/page", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == "gzip"
    # httpx decodes transparently, so compare the wire length to the body.
    assert int(r.headers["content-length"]) < len(BIG) / 2, (
        "the response is not meaningfully smaller on the wire"
    )


def test_the_live_update_stream_is_never_compressed():
    """The one that must not regress. A compressed event stream still returns
    200 with plausible-looking headers -- it just stops arriving promptly, so
    the failure shows up as a mission page that has quietly stopped updating
    rather than as anything that looks like an error."""
    with TestClient(_app()) as client:
        r = client.get("/events", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert "content-encoding" not in r.headers, (
        "the event stream is being compressed; live updates will stall"
    )


def test_the_dashboard_app_has_compression_installed():
    """The middleware is on the real app, not just possible in principle.

    Read rather than imported. Importing the server module here re-resolves
    the data paths inside whichever parallel worker happens to run this file,
    which left every other test in that worker without an initialised store --
    a failure that appeared in unrelated suites and pointed nowhere near here.
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "server.py").read_text()
    assert "Middleware(GZipMiddleware" in src, (
        "the dashboard serves everything uncompressed again"
    )
