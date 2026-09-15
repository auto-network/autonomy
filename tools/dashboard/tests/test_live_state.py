"""/api/vault/organizations: the running worker's per-org state, collected
in-process (tools/dashboard/live_state.py). State names and counts only."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tools.dashboard import live_state, link_serving_supervisor as sup, unlock_routes


def test_collect_reports_held_keys_delegates_membership_and_connectors(monkeypatch):
    cache = SimpleNamespace(secrets={"s1": b"", "s2": b"", "p1": b""})
    monkeypatch.setattr(unlock_routes, "_VAULT_CACHE", {
        "cache": cache, "organization_kem_keys": {"g-acme": {"k": 1}},
    })
    monkeypatch.setattr("tools.graph.settings_ops.personal_delegate_audited_is_warm", lambda: True)
    monkeypatch.setattr(sup, "_discover_startup_orgs", lambda: ["personal", "acme", "beta"])
    monkeypatch.setattr(sup, "serve_cert_state",
                        lambda org, now=None: {"status": "ok" if org != "beta" else "missing"})
    monkeypatch.setattr(live_state, "_genesis_for",
                        lambda org: {"acme": "g-acme", "beta": "g-beta"}[org])
    monkeypatch.setattr(live_state, "_org_generation_state_ids",
                        lambda org: {"acme": {"s1", "s2", "s3"}, "beta": None}[org])
    monkeypatch.setattr(live_state, "_membership",
                        lambda org, genesis: {"capable": org == "acme", "members": 2,
                                              "in_member_set": True})
    monkeypatch.setattr(
        "tools.dashboard.org_storage_delegate.status",
        lambda *, warm, now_ms: {"organizations": [
            {"org": "acme", "status": "ready", "days_remaining": 12}]},
    )

    def control(org, op, args, timeout=12.0):
        assert op == "connector-status"
        if org == "beta":
            raise sup.TunnelUnavailable("no listener", kind="no-listener")
        return {"ok": True, "serving": org == "personal", "boot_commit": "abc",
                "fleet_runtime_configured": True, "active_streams": 0,
                "tunnel": {"connected_since": 1.0 if org == "personal" else None}}
    monkeypatch.setattr(sup, "control", control)

    out = live_state.collect(now_ms=0)
    assert out["audited_delegate_warm"] is True
    assert out["personal_generation_keys_open"] == 3
    by = {row["org"]: row for row in out["organizations"]}
    assert set(by) == {"personal", "acme", "beta"}
    assert "generation_keys" not in by["personal"]
    assert by["personal"]["connector"]["serving"] is True
    assert by["acme"]["generation_keys"] == {"recorded": 3, "open_in_worker": 2}
    assert by["acme"]["organization_kem_key_held"] is True
    assert by["acme"]["delegate"]["status"] == "ready"
    assert by["acme"]["membership"]["capable"] is True
    assert by["acme"]["connector"]["serving"] is False
    assert by["acme"]["connector"]["tunnel"] == {"connected_since": None}
    assert by["beta"]["generation_keys"] == {"recorded": None, "open_in_worker": None}
    assert by["beta"]["organization_kem_key_held"] is False
    assert by["beta"]["delegate"] == {"status": "missing"}
    assert by["beta"]["serve_cert"] == "missing"
    assert by["beta"]["connector"] == {"reachable": False, "detail": "no listener",
                                       "kind": "no-listener"}


def test_collect_never_raises_when_everything_is_cold(monkeypatch):
    monkeypatch.setattr(unlock_routes, "_VAULT_CACHE", {})
    monkeypatch.setattr("tools.graph.settings_ops.personal_delegate_audited_is_warm",
                        lambda: (_ for _ in ()).throw(RuntimeError("cold")))
    monkeypatch.setattr(sup, "_discover_startup_orgs", lambda: ["personal"])
    monkeypatch.setattr(sup, "serve_cert_state",
                        lambda org, now=None: (_ for _ in ()).throw(RuntimeError("no store")))
    monkeypatch.setattr(sup, "control",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("refused")))
    monkeypatch.setattr("tools.dashboard.org_storage_delegate.status",
                        lambda **k: {"organizations": []})
    out = live_state.collect(now_ms=0)
    assert out["audited_delegate_warm"] is False
    assert out["personal_generation_keys_open"] == 0
    [row] = out["organizations"]
    assert row["serve_cert"].startswith("unreadable")
    assert row["connector"]["reachable"] is False


# ── the route is the operator's: browser session or local bearer, nothing else ──


def _req(principal):
    from starlette.requests import Request

    return Request({"type": "http", "method": "GET", "path": "/api/vault/organizations",
                    "headers": [], "query_string": b"",
                    "state": {"api_principal": principal}})


async def _get(principal, monkeypatch):
    from tools.dashboard import api_auth, server

    monkeypatch.setattr(live_state, "collect", lambda: {"pid": 1, "organizations": []})
    # The compatibility exception (an unenrolled dashboard admits the browser
    # without a cookie) is not what is under test: the gate is enforced here.
    monkeypatch.setattr("tools.dashboard.unlock_routes.gate_enforced", lambda: True)
    return await server.api_vault_organizations(_req(principal))


@pytest.mark.asyncio
async def test_route_refuses_a_credential_less_caller(monkeypatch):
    from tools.dashboard import api_auth

    resp = await _get(api_auth.COMPATIBILITY_PRINCIPAL, monkeypatch)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_route_refuses_an_org_bound_agent(monkeypatch):
    from tools.dashboard import api_auth

    agent = api_auth.ApiPrincipal(api_auth.ApiPrincipalKind.ORG_SESSION, subject="s", org="anchore")
    resp = await _get(agent, monkeypatch)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_route_admits_the_operator_cookie_and_a_local_session(monkeypatch):
    from tools.dashboard import api_auth

    for kind in (api_auth.ApiPrincipalKind.OPERATOR_COOKIE, api_auth.ApiPrincipalKind.LOCAL_SESSION):
        resp = await _get(api_auth.ApiPrincipal(kind, subject="s"), monkeypatch)
        assert resp.status_code == 200, kind
