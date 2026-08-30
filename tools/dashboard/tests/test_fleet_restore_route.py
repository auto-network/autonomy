"""POST /api/fleet/restore restarts the serving connectors — the server half of
the profile tray's restart button (bead auto-sdrsa) — and the connector counts
the pulls it refuses while unarmed, so the sync flag can say how many."""

import asyncio

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard import fleet_enrollment_routes
from tools.dashboard import link_serving_supervisor as sup
from tools.dashboard import unlock_routes
from tools.network import fleet_relay_sync


class _FakeSupervisor:
    def __init__(self):
        self.restarted = []

    def restart(self, org):
        self.restarted.append(org)
        return {"running": True, "reason": None}


@pytest.fixture
def client(monkeypatch):
    # Operator-gated route: satisfy the unlocked-session check.
    monkeypatch.setattr(unlock_routes, "session_from_request",
                        lambda _request: object())
    with TestClient(Starlette(routes=fleet_enrollment_routes.ROUTES)) as c:
        yield c


def test_restore_restarts_set_up_serving_scopes_only(client, monkeypatch):
    fake = _FakeSupervisor()
    monkeypatch.setattr(sup, "get_supervisor", lambda: fake)
    monkeypatch.setattr(sup, "_discover_startup_orgs",
                        lambda: [None, "anchore", "blindhash"])
    status = {None: "ok", "anchore": "expired", "blindhash": "missing"}
    monkeypatch.setattr(sup, "serve_cert_state",
                        lambda org, **k: {"status": status[org]})

    resp = client.post("/api/fleet/restore", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    # personal (None) + anchore restart; blindhash (never set up) is skipped.
    assert body["restarted"] == ["personal", "anchore"]
    assert fake.restarted == [None, "anchore"]


def test_restore_requires_an_operator_session(monkeypatch):
    monkeypatch.setattr(unlock_routes, "session_from_request",
                        lambda _request: None)
    with TestClient(Starlette(routes=fleet_enrollment_routes.ROUTES)) as c:
        resp = c.post("/api/fleet/restore", json={})
    assert resp.status_code == 401


def test_one_scope_failing_does_not_stall_the_others(client, monkeypatch):
    class _Flaky(_FakeSupervisor):
        def restart(self, org):
            if org == "anchore":
                raise RuntimeError("boom")
            return super().restart(org)

    fake = _Flaky()
    monkeypatch.setattr(sup, "get_supervisor", lambda: fake)
    monkeypatch.setattr(sup, "_discover_startup_orgs", lambda: ["anchore", "autonomy"])
    monkeypatch.setattr(sup, "serve_cert_state", lambda org, **k: {"status": "ok"})

    body = client.post("/api/fleet/restore", json={}).json()
    assert body["restarted"] == ["autonomy"]
    assert [f["scope"] for f in body["failed"]] == ["anchore"]


def test_connector_counts_refusals_while_unarmed():
    rt = fleet_relay_sync.ConnectorFleetRuntime()
    assert rt.scheduler is None
    assert rt.locked_refusals == 0
    for _ in range(3):
        with pytest.raises(fleet_relay_sync.FleetRelaySyncError):
            asyncio.run(rt.handle("token", {}))
    assert rt.locked_refusals == 3
    assert rt.first_locked_refusal_at is not None
