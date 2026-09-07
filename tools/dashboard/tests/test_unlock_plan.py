"""The unlock plan: one round-trip, and NO second copy of any rule.

`GET /api/network/unlock-plan` is the to-do list the root ceremony gates its
per-org steps on (design graph://914adcc6-b77). Its whole value is that the
ceremony stops probing — so if a verdict here were re-derived rather than
resolved from the rule's owner, the ceremony would gate on a different answer
than the owner gives, and the probe storm would have been traded for a
divergence. That is the failure these tests exist to prevent:

* the plan's serving verdict IS `serve_cert_requirement`, the same object the
  `/api/network/serve-cert` route returns — pinned by monkeypatching the one
  definition and watching BOTH surfaces move together;
* the renewal rule that a simpler copy would drop: a credential that is still
  valid (`status == "ok"`) but inside the renewal window is REQUIRED, as is one
  whose row carries no `dns01_cert`. Gating on `status != "ok"` would skip both
  and let a live credential die;
* a local/personal store is never a committed-membership org, so the ceremony
  never attempts a checkpoint or a persona-signed serving act for it.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from tools.dashboard import network_routes
from tools.dashboard import link_serving_supervisor as lss

ORG = "personal"


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    monkeypatch.setenv("AUTONOMY_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.setenv("GRAPH_ORG", ORG)
    (tmp_path / "orgs").mkdir(parents=True, exist_ok=True)
    from tools.graph.db import GraphDB
    # This fixture repoints the graph/org stores; tools.graph.db pools
    # connections by path, so a pooled handle to this temp dir would leak into
    # the next module in the same xdist worker. Drain on both edges.
    GraphDB.close_all_pooled()
    app = Starlette(routes=[
        Route("/api/network/unlock-plan",
              network_routes.get_unlock_plan, methods=["GET"]),
        Route("/api/network/serve-cert",
              network_routes.get_serve_cert_status, methods=["GET"]),
    ])
    try:
        with TestClient(app) as c:
            yield c
    finally:
        GraphDB.close_all_pooled()


def _plan(client, orgs=ORG):
    r = client.get("/api/network/unlock-plan", params={"orgs": orgs})
    assert r.status_code == 200, r.text
    return r.json()["plan"]


def test_orgs_are_required():
    # Without a slug list there is no plan to compute — refused, not empty.
    monkey = TestClient(Starlette(routes=[
        Route("/api/network/unlock-plan",
              network_routes.get_unlock_plan, methods=["GET"])]))
    assert monkey.get("/api/network/unlock-plan").status_code == 400


def test_serving_verdict_is_the_status_routes_verdict(client, monkeypatch):
    """THE ONE THAT MATTERS. Both surfaces must resolve the SAME function, so
    the ceremony's gate cannot disagree with the route that owns the rule."""
    sentinel = {"required": True, "status": "expired", "days_remaining": -3.0}
    monkeypatch.setattr(lss, "serve_cert_requirement", lambda org, **kw: sentinel)

    assert _plan(client)[ORG]["serve_cert"] == sentinel
    assert client.get("/api/network/serve-cert").json() == sentinel


def test_a_credential_inside_the_renewal_window_is_still_due(monkeypatch):
    """`status == "ok"` is NOT "nothing to do". A copy of the rule written as
    `status != "ok"` would skip this renewal and the credential would die."""
    now = 1_800_000_000
    in_ten_days = now + 10 * 86400
    monkeypatch.setattr(lss, "serve_cert_state", lambda org, **kw: {
        "status": "ok",
        "row": {"not_after": in_ten_days, "dns01_cert": "x"},
    })
    verdict = lss.serve_cert_requirement(ORG, now=now)

    assert verdict["status"] == "ok"          # serving is NOT interrupted
    assert verdict["required"] is True        # ...but the unlock must renew
    assert verdict["days_remaining"] == 10.0


def test_a_credential_outside_the_window_is_not_due(monkeypatch):
    now = 1_800_000_000
    monkeypatch.setattr(lss, "serve_cert_state", lambda org, **kw: {
        "status": "ok",
        "row": {"not_after": now + 29 * 86400, "dns01_cert": "x"},
    })
    assert lss.serve_cert_requirement(ORG, now=now)["required"] is False


def test_a_pre_narrowing_credential_is_due_even_when_fresh(monkeypatch):
    """No dns01_cert means the ceremony still owes this org an upgrade."""
    now = 1_800_000_000
    monkeypatch.setattr(lss, "serve_cert_state", lambda org, **kw: {
        "status": "ok", "row": {"not_after": now + 29 * 86400},
    })
    assert lss.serve_cert_requirement(ORG, now=now)["required"] is True


def test_a_local_store_is_never_a_committed_membership_org(client):
    """The personal store has no registry binding and no org key, so the
    ceremony must not attempt a checkpoint or a persona-signed serving act for
    it — the plan says so up front rather than the client discovering it by
    failing a call per unlock."""
    entry = _plan(client)[ORG]

    assert entry["committed_membership_org"] is False
    assert entry["checkpoint"]["needed"] is False
    assert entry["checkpoint"]["checkpointer_pubs"] == []


def test_one_bad_slug_cannot_cost_the_others(client):
    """ONE SLUG CANNOT TAKE DOWN THE PLAN. A slug whose store does not exist
    raises out of the first settings read; unisolated, that 500s the whole
    request and the ceremony is left with no verdict for ANY org (falling back
    to probing all of them). Same isolation rule as the step runner."""
    plan = _plan(client, orgs="no-such-org," + ORG)

    assert plan["no-such-org"].get("error") in ("unavailable", "scope-refused")
    # The good org still got its full verdict.
    assert plan[ORG]["committed_membership_org"] is False
    assert "serve_cert" in plan[ORG]


def test_rekey_is_reported_not_due(client):
    """There is no rekey-policy source, so the honest verdict is 'never due' —
    stated here so the ceremony stops paying a 404 per org to rediscover it."""
    assert _plan(client)[ORG]["rekey"]["due"] is False
