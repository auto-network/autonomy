"""POST /api/network/join/outcome at route level (auto-qrmlg.3).

The joiner's install: re-fold the bundled events, create the org, store the
binding and persona, seed the sponsor's addresses (install seed, never the
org sets), and adopt the bundled checkpoint by folding the installed ledger
at its head (OrgAdmission.tla rule bundle_adopt; lemma AdoptedIsAuthentic:
the roster is recomputed, never trusted).
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.network.idkit import KeyPair, Subject, issue_cert
from tools.network.ledger import membership_commitment as mc
from tools.network.ledger.tests.conftest import Sim
from tools.network.ledger.tests.test_membership_commitment import add_member, org_with_owner

REGISTRY = "https://registry.test"


@pytest.fixture(autouse=True)
def _stores(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.setenv("AUTONOMY_DATA_ROOT", str(tmp_path))
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    from tools.graph.db import GraphDB
    (tmp_path / "orgs").mkdir(parents=True, exist_ok=True)
    GraphDB.close_all_pooled()
    try:
        yield
    finally:
        GraphDB.close_all_pooled()


def _client() -> TestClient:
    from tools.dashboard import network_routes
    return TestClient(Starlette(routes=network_routes.ROUTES))


def _joined_org():
    """A founded org with one admitted member; returns (sim, founder persona,
    member persona, the member's claim invite ref)."""
    sim, founder = org_with_owner()
    persona, ik = KeyPair.generate(), KeyPair.generate()
    if "member" not in sim.fold().role_defs:
        sim.role_define(sim.root, "member", ["link:publish"], requires="self")
    invite_id = sim.invite(sim.root, "member", invite_key=ik)
    sim.claim(invite_id, ik, persona)
    return sim, founder, persona, invite_id


def _genesis_org(sim) -> str:
    return sim.ledger.get(sim.genesis_id).payload["org"]


def _state_of(sim, seq: int) -> dict:
    state = sim.fold()
    head = sorted(state.heads)[0] if state.heads else sim.genesis_id
    return {"seq": seq, "members_root": mc.members_root(state),
            "checkpointers_root": mc.checkpointers_root(state), "ledger_head": head}


def _body(sim, persona, invite_id, **extra) -> dict:
    org_uuid = _genesis_org(sim)
    events = sorted(sim.ledger.events(), key=lambda e: (e.type != "genesis", e.hlc.ts, e.hlc.count))
    body = {
        "org_uuid": org_uuid, "genesis_id": sim.genesis_id, "invite_ref": invite_id,
        "persona_pub": persona.public_hex, "org_name": "Boatlore",
        "events": [e.to_json().decode("utf-8") for e in events],
        "binding": {"org_uuid": org_uuid, "root_pub": sim.root.public_hex,
                    "registry_url": REGISTRY, "recovery_policy": {"mode": "none"},
                    "binding_generation": "ab" * 32,
                    "binding_expires_at": "2030-01-01T00:00:00Z"},
        "member_profiles": [],
    }
    body.update(extra)
    return body


def _sponsor_reachability_row(sim, sponsor_persona: KeyPair) -> dict:
    """A verifiable reachability row for one of the sponsor's machines: its
    machine key certified by the sponsor persona for fleet:sync in this org."""
    from tools.network.fleet_org_reachability import build_row
    machine = KeyPair.generate()
    now = int(time.time())
    cert = issue_cert(
        sponsor_persona, machine.public_hex, scope=("fleet:sync",), org=sim.genesis_id,
        subject=Subject("persona", sponsor_persona.public_hex),
        not_before=now - 300, not_after=now + 86_400,
    )
    # Served entries are {"key": machine_pub, **row}: the stored row does not
    # repeat the machine key (claim_service._reachability_rows).
    return {"key": machine.public_hex, **build_row(machine, cert, ["ws://10.0.0.7:8477"], now=now)}


def test_install_adopts_the_bundled_checkpoint_by_fold(monkeypatch):
    sim, _founder, persona, invite_id = _joined_org()
    r = _client().post("/api/network/join/outcome", json=_body(sim, persona, invite_id, checkpoint=_state_of(sim, 1)))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["ok"] is True and out["checkpoint"] == {"ok": True, "action": "adopted", "seq": 1}
    from tools.dashboard import membership_checkpoint as cp
    adopted = cp._cached_adopted(out["org"])
    assert adopted["seq"] == 1 and adopted["members_root"] == mc.members_root(sim.fold())
    # The adopted set includes the joiner: it can prove membership under it.
    members = mc.member_pubs(sim.fold())
    assert persona.public_hex in members


def test_install_refuses_a_bundled_root_the_ledger_does_not_produce():
    sim, _founder, persona, invite_id = _joined_org()
    forged = dict(_state_of(sim, 1), members_root="f" * 64)
    r = _client().post("/api/network/join/outcome", json=_body(sim, persona, invite_id, checkpoint=forged))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["ok"] is True  # the organization IS installed
    assert out["checkpoint"]["ok"] is False and "does not match" in out["checkpoint"]["error"]
    from tools.dashboard import membership_checkpoint as cp
    assert cp._cached_adopted(out["org"]) is None


def test_install_without_a_bundled_checkpoint_reports_it():
    sim, _founder, persona, invite_id = _joined_org()
    r = _client().post("/api/network/join/outcome", json=_body(sim, persona, invite_id, checkpoint=None))
    out = r.json()
    assert out["ok"] is True
    assert out["checkpoint"] == {"ok": False, "error": "join bundle's carried no membership checkpoint"}


def test_install_seeds_the_sponsor_addresses_it_was_sent():
    sim, founder, persona, invite_id = _joined_org()
    row = _sponsor_reachability_row(sim, founder)
    r = _client().post("/api/network/join/outcome", json=_body(
        sim, persona, invite_id, checkpoint=_state_of(sim, 1), reachability_rows=[row]))
    assert r.status_code == 200, r.text
    slug = r.json()["org"]
    from tools.dashboard.org_install_seed import seed_reachability
    seeded = seed_reachability(slug, org=sim.genesis_id)
    assert seeded == {row["key"]: ["ws://10.0.0.7:8477"]}
