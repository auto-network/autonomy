"""Static assets are cacheable when the URL names a build, and not otherwise.

Nothing under /static carried a cache-control header, so a browser had no
instruction to reuse anything and revalidated on every navigation. The shell
requests nineteen files before it can paint, so a page change cost nineteen
conditional requests, each a round trip, six at a time over HTTP/1.1, sharing
that budget with the event streams the pages keep open. Every one answers 304
with an empty body: nothing is transferred and the reader waits regardless.
"""

from __future__ import annotations

from pathlib import Path

from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

from tools.dashboard.server import _VersionedStatic


def _client(tmp_path: Path) -> TestClient:
    (tmp_path / "app.js").write_text("console.log(1)")
    app = Starlette(routes=[
        Mount("/static", app=_VersionedStatic(directory=str(tmp_path))),
    ])
    return TestClient(app)


def test_an_asset_asked_for_by_build_may_be_kept(tmp_path):
    """The shell stamps every asset it references with the newest change time
    under static/, so a changed file is a different URL and can never be
    served from a stale copy."""
    with _client(tmp_path) as client:
        r = client.get("/static/app.js?v=1786800000")
    assert r.status_code == 200
    cache = r.headers["cache-control"]
    assert "immutable" in cache and "max-age=31536000" in cache, cache


def test_an_asset_asked_for_without_a_build_is_rechecked_every_time(tmp_path):
    """Five references carry no stamp, the encryption library among them.
    Keeping those would strand a browser on whichever build it first saw,
    with no URL change to release it."""
    with _client(tmp_path) as client:
        r = client.get("/static/app.js")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"


def test_a_kept_asset_is_not_merely_revalidated(tmp_path):
    """The failure this replaces answered 304 to a conditional request, which
    looks fine in a log and still costs the round trip that was the problem."""
    with _client(tmp_path) as client:
        first = client.get("/static/app.js?v=1786800000")
        again = client.get(
            "/static/app.js?v=1786800000",
            headers={"If-None-Match": first.headers["etag"]},
        )
    assert again.status_code == 304
    assert "immutable" in first.headers["cache-control"], (
        "without this the browser reissues the request above on every "
        "navigation instead of reading its own copy"
    )
