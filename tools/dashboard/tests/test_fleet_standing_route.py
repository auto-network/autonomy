"""The serving machine's self-minted standing fleet sync route."""

from __future__ import annotations

import pytest

from tools.dashboard import fleet_standing_route as fsr
from tools.dashboard.link_serving import check_grant
from tools.graph.db import GraphDB
from tools.network import fleet_route

MACHINE_ID = "ab" * 32
MACHINE_PUB = "cd" * 32
TOKEN_A = "11" * 16
TOKEN_B = "22" * 16


@pytest.fixture
def machine(tmp_path, monkeypatch):
    orgs = tmp_path / "orgs"
    orgs.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    GraphDB.close_all_pooled()
    GraphDB.create_org_db("personal", type_="personal").close()
    GraphDB.create_org_db(
        "machine", type_="personal", path=orgs.parent / "machine.db"
    ).close()
    yield tmp_path
    GraphDB.close_all_pooled()


def _minter(tokens: list[str], calls: list):
    def create_link(org, args):
        calls.append((org, args))
        token = tokens.pop(0)
        return {"ok": True, "token": token, "url": f"https://relay.example/l/{token}"}
    return create_link


def test_mints_once_caches_the_grant_and_stores_the_self_route(machine):
    calls: list = []
    route = fsr.ensure_standing_route(
        machine_id=MACHINE_ID, machine_pub=MACHINE_PUB,
        create_link=_minter([TOKEN_A, TOKEN_B], calls),
        relay_check=lambda _rv: True,
    )
    assert route == fleet_route.FleetRoute(
        f"https://relay.example/l/{TOKEN_A}", MACHINE_PUB
    )
    assert fleet_route.load_self(org="machine") == route
    # the mint asked for a non-expiring fleet:sync link with a label only
    (org, args), = calls
    assert org == "personal"
    assert args["target_type"] == "fleet:sync"
    assert args["meta"] == {"label": fsr.STANDING_ROUTE_LABEL}
    assert "ttl" not in args["meta"]
    # the local I9 gate admits it, typed as a sync route, attributed to the machine
    grant = check_grant(TOKEN_A, org=None)
    assert grant["target_type"] == "fleet:sync"
    assert grant["subject"] == {"kind": "machine", "id": MACHINE_ID}
    assert "ttl" not in grant["meta"]

    # second call: nothing minted, same route
    again = fsr.ensure_standing_route(
        machine_id=MACHINE_ID, machine_pub=MACHINE_PUB,
        create_link=_minter([TOKEN_B], calls),
        relay_check=lambda _rv: True,
    )
    assert again == route
    assert len(calls) == 1


def test_relay_that_forgot_the_route_triggers_a_remint(machine):
    calls: list = []
    first = fsr.ensure_standing_route(
        machine_id=MACHINE_ID, machine_pub=MACHINE_PUB,
        create_link=_minter([TOKEN_A, TOKEN_B], calls),
        relay_check=lambda _rv: True,
    )
    second = fsr.ensure_standing_route(
        machine_id=MACHINE_ID, machine_pub=MACHINE_PUB,
        create_link=_minter([TOKEN_B], calls),
        relay_check=lambda _rv: False,   # relay answered 404
    )
    assert first != second
    assert second.rendezvous.endswith(TOKEN_B)
    assert fleet_route.load_self(org="machine") == second
    assert len(calls) == 2


def test_an_unreachable_relay_never_remints(machine):
    calls: list = []
    first = fsr.ensure_standing_route(
        machine_id=MACHINE_ID, machine_pub=MACHINE_PUB,
        create_link=_minter([TOKEN_A], calls),
        relay_check=lambda _rv: True,
    )
    again = fsr.ensure_standing_route(
        machine_id=MACHINE_ID, machine_pub=MACHINE_PUB,
        create_link=_minter([TOKEN_B], calls),
        relay_check=lambda _rv: None,   # relay offline: unknown, not "no"
    )
    assert again == first
    assert len(calls) == 1


def test_relay_refusal_and_malformed_replies_raise(machine):
    with pytest.raises(fsr.StandingRouteError):
        fsr.ensure_standing_route(
            machine_id=MACHINE_ID, machine_pub=MACHINE_PUB,
            create_link=lambda org, args: {"ok": False, "error": "nope"},
        )
    with pytest.raises(fsr.StandingRouteError):
        fsr.ensure_standing_route(
            machine_id=MACHINE_ID, machine_pub=MACHINE_PUB,
            create_link=lambda org, args: {"ok": True, "token": "zz", "url": "https://x/l/zz"},
        )
    assert fleet_route.load_self(org="machine") is None
