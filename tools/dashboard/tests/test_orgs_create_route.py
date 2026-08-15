"""POST /api/orgs — the org-SHELL route contract (auto-jdba4, I1).

The route creates the organization shell and nothing else. It takes no
passphrase: the org root is generated and the founding batch signed in the
operator's browser, then folded by ``POST /api/network/ledger/found``. The
enumeration guard in ``test_i1_no_passphrase_ingress.py`` holds the wire rule
for every route; these tests hold this route's behaviour.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from tools.graph import org_ops


@pytest.fixture
def client(tmp_path, monkeypatch):
    from tools.graph.db import GraphDB
    from tools.dashboard import server

    GraphDB.close_all_pooled()
    # Orgs-tree hermeticity, no GRAPH_DB pin: shell creation writes to the
    # created org's OWN db, which a pin would contradict and the fail-loud
    # resolver refuses. delenv guards ambient leaks.
    orgs = tmp_path / "orgs"
    orgs.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    GraphDB.close_all_pooled()
    GraphDB.create_org_db(
        "personal", type_="personal", path=orgs / "personal.db"
    ).close()
    app = Starlette(routes=[Route("/api/orgs", server.api_orgs_create, methods=["POST"])])
    with TestClient(app) as c:
        yield c
    GraphDB.close_all_pooled()


def test_missing_slug_is_400(client):
    r = client.post("/api/orgs", json={})
    assert r.status_code == 400
    assert "slug" in r.json()["error"]


def test_creating_a_shell_needs_no_passphrase(client):
    """The whole point of auto-jdba4: founding no longer costs a password."""
    r = client.post("/api/orgs", json={"slug": "acme"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["org"]["slug"] == "acme"
    # The stable orgs.id the browser must bind into genesis (D21).
    assert body["org"]["id"]
    # The shell is explicitly NOT founded -- the browser does that next.
    assert body["founded"] is False
    assert any(o.slug == "acme" for o in org_ops.list_orgs())


def test_no_identity_needs_to_be_enrolled_server_side(client):
    """No personal identity exists in this fixture, and the shell still creates.

    The personal root is the browser's business now; the server has no reason
    to consult it, which is exactly why it no longer holds the passphrase.
    """
    assert client.post("/api/orgs", json={"slug": "shellonly"}).status_code == 201


def test_a_passphrase_in_the_body_is_never_used(client):
    """A stale client sending the old field must not get the old behaviour.

    It is not an error -- the field is simply not part of the contract -- but
    nothing may decrypt with it, and the result is the same inert shell.
    """
    r = client.post(
        "/api/orgs", json={"slug": "acme", "personal_password": "anything-at-all"}
    )
    assert r.status_code == 201, r.text
    assert r.json()["founded"] is False


def test_an_unfounded_shell_is_a_retryable_target(client):
    """The two-call window (auto-jdba4): a failed founding strands nothing."""
    first = client.post("/api/orgs", json={"slug": "acme"})
    assert first.status_code == 201
    # The browser's founding call failed / the tab closed. Try again.
    again = client.post("/api/orgs", json={"slug": "acme"})
    assert again.status_code == 201, again.text
    # Same organization, same stable id -- so a genesis signed against the
    # first response is still valid against this one.
    assert again.json()["org"]["id"] == first.json()["org"]["id"]


def test_invalid_slug_is_400(client):
    assert client.post("/api/orgs", json={"slug": "Not A Slug"}).status_code == 400
