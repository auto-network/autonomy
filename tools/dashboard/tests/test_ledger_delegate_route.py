"""The delegate event has to become durable, or every later write fails.

The unattended agent delegate is minted in the BROWSER, where the persona key
lives — this process may hold the delegate's signing key but never the
persona's, so it cannot mint one itself. The browser posts the signed event
here to be made durable.

Durability is the entire point of the route. A delegate that exists only in
the process that minted it resolves against that process's fold and nowhere
else, so the first thing to re-open the ledger — a key holder, a sealer —
cannot resolve the author to a member. The write then fails with an authority
error that names the wrong cause: it reads as "this delegate is not
authorized" when the truth is "this delegate was never written down".
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from tools.dashboard import network_routes


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    monkeypatch.setenv("AUTONOMY_DATA_ROOT", str(tmp_path))
    # The caller's own org, per the scope cascade _scoped_org resolves
    # through. Without it every request is a cross-org attempt and is
    # correctly refused 403 before any of this route's own checks run.
    monkeypatch.setenv("GRAPH_ORG", "personal")
    app = Starlette(routes=[
        Route("/api/network/ledger/delegate",
              network_routes.post_ledger_delegate, methods=["POST"]),
    ])
    with TestClient(app) as c:
        yield c


def _post(client, **body):
    return client.post("/api/network/ledger/delegate", json=body)


def test_a_missing_org_is_refused(client):
    assert _post(client, event="{}").status_code == 400


def test_a_missing_event_is_refused(client):
    assert _post(client, org="personal").status_code == 400


def test_garbage_is_refused_before_anything_is_appended(client):
    r = _post(client, org="personal", event="not an event at all")

    assert r.status_code == 400
    assert "rejected" in r.json()["error"]


def test_a_real_delegate_event_becomes_DURABLE(client, tmp_path):
    """THE ONE THAT MATTERS. The whole route exists so the delegate survives a
    re-open — a delegate held only in the minting process resolves against
    that process's fold and nowhere else, and every later write fails with an
    authority error naming the wrong cause.
    """
    from tools.network.idkit import KeyPair
    from tools.network.idkit.persona import derive_persona
    from tools.network.ledger import HLC, LedgerStore, org_ledger_db_path
    from tools.network.ledger.found import found_org_ledger
    from tools.network.storagekit import credentials as cm, delegate as delegate_mod

    seed, root, T0 = bytes(range(32)), KeyPair.generate(), 1_800_000_000_000
    path = org_ledger_db_path("personal")
    with LedgerStore(path) as store:
        res = found_org_ledger(store, org_id="personal", org_root=root,
                               personal_root_seed=seed, now=T0,
                               kem_seed=cm.derive_kem_seed(seed))
        founder = derive_persona(seed, res.genesis_id)
        before = len(store)

    # Mint against a SEPARATE view so the event is genuinely new to the store
    # the route will append it to — which is what a browser-minted event is.
    with LedgerStore(path) as side:
        agent = delegate_mod.provision(side.ledger, founder, founder,
                                       res.genesis_id, hlc=HLC(T0 + 1000, 0),
                                       ttl_ms=3_600_000)
        wire = side.ledger.get(agent.grant_event_id).to_json().decode()

    response = _post(client, org="personal", event=wire)
    assert response.status_code == 200, response.text

    with LedgerStore(path) as reopened:
        assert len(reopened) == before + 1, "the delegate did not survive"
        from tools.network.storagekit.acceptance import resolve_member_key
        assert resolve_member_key(reopened.fold(), agent.public_hex) is not None, (
            "the delegate is on disk but does not resolve to a member")
