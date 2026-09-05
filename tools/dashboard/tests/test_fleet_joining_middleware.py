"""_FleetJoiningMiddleware redirects a mid-join machine, but not one that has
already enrolled.

The middleware sits outside the unlock gate. While it redirects every page to
``/`` and the unlock gate redirects ``/`` back out, a stuck marker produces a
redirect loop that makes the dashboard entirely unusable. A machine that holds
a durable identity has finished enrolling, so the middleware must treat it as
not joining regardless of a stale marker — that is what stops the lockout.
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from tools.dashboard.server import _FleetJoiningMiddleware
from tools.network import machine_boot


async def _page(request):
    return PlainTextResponse("machines page")


def _client():
    app = Starlette(
        routes=[
            Route("/machines", _page),
            Route("/unlock", _page),
            Route("/welcome", _page),
        ],
        middleware=[Middleware(_FleetJoiningMiddleware)],
    )
    return TestClient(app)


def test_joining_without_identity_redirects_to_home(monkeypatch):
    monkeypatch.setattr(machine_boot, "machine_id", lambda **_: None)
    monkeypatch.setattr(machine_boot, "is_joining", lambda **_: True)
    resp = _client().get("/machines", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/"


def test_stale_marker_with_identity_does_not_redirect(monkeypatch):
    """A stale marker cannot lock out a machine that already has an identity."""
    monkeypatch.setattr(machine_boot, "machine_id", lambda **_: "01" * 32)
    monkeypatch.setattr(machine_boot, "is_joining", lambda **_: True)
    resp = _client().get("/machines", follow_redirects=False)
    assert resp.status_code == 200
    assert resp.text == "machines page"


def test_not_joining_without_identity_does_not_redirect(monkeypatch):
    monkeypatch.setattr(machine_boot, "machine_id", lambda **_: None)
    monkeypatch.setattr(machine_boot, "is_joining", lambda **_: False)
    resp = _client().get("/machines", follow_redirects=False)
    assert resp.status_code == 200


def test_unlock_and_welcome_are_reachable_mid_join(monkeypatch):
    """The completion flow must not be funnelled home. Once enrollment delivers
    the armor, the human gate sends '/' -> '/unlock' and the welcome page's own
    button targets '/unlock' (next=/welcome); bouncing those to '/' deadlocks
    the join. They are exempt even while genuinely joining with no identity."""
    monkeypatch.setattr(machine_boot, "machine_id", lambda **_: None)
    monkeypatch.setattr(machine_boot, "is_joining", lambda **_: True)
    client = _client()
    for path in ("/unlock", "/welcome"):
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 200, path
        assert resp.text == "machines page"
