"""The broker as a plugin: it loads, it is gated by its toggle, its
reconciler waits for worker activation, and it puts the globe on sessions."""

import asyncio
import sys
import time
import types

import pytest

from tools.dashboard.plugin_api import loader
from tools.dashboard.plugins.browser import store
from tools.dashboard.plugins.browser.entrypoints import api, background
from tools.dashboard.dao import dashboard_db


def _browser_plugin():
    plugins = {p.id: p for p in loader.load_all()}
    return plugins["browser"]


def test_the_plugin_loads_with_every_route_the_core_module_served():
    from starlette.routing import WebSocketRoute

    plugin = _browser_plugin()
    assert plugin.manifest.default_enabled is True
    paths = {(getattr(r, "path", None), type(r).__name__) for r in plugin.routes}
    assert ("/browser", "Route") in paths and ("/browser/{lease}", "Route") in paths
    assert ("/ws/browser/{lease}/view", "WebSocketRoute") in paths
    assert {p for p, _ in paths if p.startswith("/api/browser/")} == {
        "/api/browser/leases", "/api/browser/leases/{lease}",
        "/api/browser/leases/{lease}/commands", "/api/browser/leases/{lease}/secure-login",
        "/api/browser/operator/leases", "/api/browser/leases/{lease}/control"}
    assert any(isinstance(r, WebSocketRoute) for r in plugin.routes)
    assert plugin.session_contributions is api.session_contributions
    assert plugin.background is background.tasks


def test_a_disabled_plugin_answers_404_on_its_api_but_the_websocket_keeps_its_own_checks():
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from tools.dashboard import api_auth, route_policy
    from tools.dashboard.api_auth import ApiPrincipal, ApiPrincipalKind

    enabled = {"browser": False}
    routes = route_policy.apply_default_deny(
        route_policy.gate_plugin_enabled("browser", api.ROUTES, lambda: enabled), plugin=True)
    client = TestClient(Starlette(routes=routes), base_url="https://dash.example")

    original = api_auth.principal_from_request
    try:
        api_auth.principal_from_request = lambda request: ApiPrincipal(
            kind=ApiPrincipalKind.OPERATOR_COOKIE)
        assert client.get("/api/browser/operator/leases").status_code == 404
        api_auth.principal_from_request = lambda request: ApiPrincipal(
            kind=ApiPrincipalKind.COMPATIBILITY)
        assert client.get("/api/browser/operator/leases").status_code == 401
    finally:
        api_auth.principal_from_request = original


def test_the_reconciler_waits_for_worker_activation(monkeypatch):
    started = []
    fake_reconciler = types.SimpleNamespace(run_forever=lambda: _record(started))
    monkeypatch.setitem(sys.modules, "tools.dashboard.plugins.browser.reconciler", fake_reconciler)
    import tools.dashboard.plugins.browser as package
    monkeypatch.setattr(package, "reconciler", fake_reconciler, raising=False)
    server = types.SimpleNamespace(_worker_activated=False)
    monkeypatch.setitem(sys.modules, "tools.dashboard.server", server)
    monkeypatch.setattr(background, "ACTIVATION_POLL_S", 0.01)
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)

    async def scenario():
        [factory] = background.tasks()
        task = asyncio.create_task(factory())
        await asyncio.sleep(0.1)
        assert started == []                 # predecessor still serving: no epoch taken
        server._worker_activated = True
        await asyncio.wait_for(task, 1)
        assert started == [True]

    asyncio.run(scenario())


async def _record(started):
    started.append(True)


@pytest.fixture
def db(tmp_path, monkeypatch):
    dashboard_db.init_db(tmp_path / "dashboard.db")
    monkeypatch.setattr(store, "_ready_path", None)
    yield


def test_the_globe_marks_sessions_with_a_running_lease_for_the_operator_only(db, monkeypatch):
    from tools.dashboard import api_auth
    from tools.dashboard.plugin_api.session_contributions import normalize_descriptor

    epoch = store.take_epoch()
    store.admit(epoch=epoch, max_leases=4, lease_hash_="a" * 64, session="auto-one",
                org="autonomy", workspace="ws", profile_kind="ephemeral", profile_name=None,
                adapter="chrome-headed", container_name="brw-e-a", expires_at=time.time() + 600,
                secret="s" * 64, vnc_password="p@ss!wd8")
    for state in ("starting", "ready"):
        assert store.transition("a" * 64, epoch=epoch, to=state)

    operator = types.SimpleNamespace(global_authority=True)
    monkeypatch.setattr(api_auth, "principal_from_request", lambda request: operator)
    rows = api.session_contributions(["auto-one", "auto-two"], None)
    assert rows["auto-two"] == []
    [row] = rows["auto-one"]
    assert row["href"] == "/browser/" + "a" * 16 and row["accent"] == "#38bdf8"
    assert normalize_descriptor("browser", "auto-one", row) is not None

    assert store.transition("a" * 64, epoch=epoch, to="locked", lock_holder="human")
    assert api.session_contributions(["auto-one"], None)["auto-one"][0]["accent"] == "#f59e0b"

    monkeypatch.setattr(api_auth, "principal_from_request",
                        lambda request: types.SimpleNamespace(global_authority=False))
    assert api.session_contributions(["auto-one"], None)["auto-one"] == []

    monkeypatch.setattr(api_auth, "principal_from_request", lambda request: operator)
    assert store.transition("a" * 64, epoch=epoch, to="releasing")
    assert store.transition("a" * 64, epoch=epoch, to="gone")
    assert api.session_contributions(["auto-one"], None)["auto-one"] == []
