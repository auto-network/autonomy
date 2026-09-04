"""POST /api/fleet/machines/kick — verify a browser-signed kick, then refuse
anything a single operator tap should not do.

The route owns no seed: the browser mints the tombstone (fleet-kick.js) and this
verifies it against the stored personal root and the resolved roster. These
tests mint entries with a real KeyPair (so signatures genuinely verify) and stub
only the ambient state the route reads: the personal anchor, the stored roster,
the local machine id, and the serving selection.
"""

from __future__ import annotations

import types

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard import fleet_enrollment_routes
from tools.dashboard import identity_routes
from tools.dashboard import unlock_routes
from tools.network import fleet_roster, fleet_tunnel_server, machine_boot
from tools.network.idkit import KeyPair


ROOT = KeyPair.from_private_hex(bytes(range(32)).hex())
TARGET_MID = "ab" * 32
TARGET_PUB = "cd" * 32
LOCAL_MID = "11" * 32


def _enroll(seq=0):
    return fleet_roster.enroll(
        ROOT, machine_id=TARGET_MID, machine_pub=TARGET_PUB, seq=seq,
    )


def _kick(seq=1, machine_id=TARGET_MID, machine_pub=TARGET_PUB):
    return fleet_roster.kick(
        ROOT, machine_id=machine_id, machine_pub=machine_pub, seq=seq,
    )


@pytest.fixture
def env(monkeypatch):
    """Wire the route's ambient state; knobs live on the returned namespace."""
    state = types.SimpleNamespace(
        entries=[_enroll(seq=0)],
        local_machine_id=LOCAL_MID,
        selected_machine_id=None,
        stored=[],
    )
    monkeypatch.setattr(unlock_routes, "session_from_request", lambda _r: object())
    monkeypatch.setattr(
        identity_routes, "_personal_member",
        lambda: types.SimpleNamespace(payload={"root_pub": ROOT.public_hex}),
    )
    monkeypatch.setattr(fleet_roster, "load_entries", lambda org=None: list(state.entries))
    monkeypatch.setattr(
        fleet_roster, "store_entry",
        lambda entry, org=None: (state.stored.append(entry) or entry.entry_id),
    )
    monkeypatch.setattr(machine_boot, "machine_id", lambda org=None: state.local_machine_id)
    monkeypatch.setattr(
        fleet_tunnel_server, "state",
        lambda: types.SimpleNamespace(selected_machine_id=state.selected_machine_id),
    )
    with TestClient(Starlette(routes=fleet_enrollment_routes.ROUTES)) as client:
        yield client, state


def _post(client, entry):
    return client.post(
        "/api/fleet/machines/kick", json={"roster_entry": entry.to_dict()},
    )


def test_kick_happy_path_persists_the_tombstone(env):
    client, state = env
    resp = _post(client, _kick(seq=1))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["machine_id"] == TARGET_MID
    assert len(state.stored) == 1
    assert state.stored[0].kind is fleet_roster.EntryKind.KICK


def test_kick_requires_an_operator_session(monkeypatch):
    monkeypatch.setattr(unlock_routes, "session_from_request", lambda _r: None)
    with TestClient(Starlette(routes=fleet_enrollment_routes.ROUTES)) as client:
        resp = client.post(
            "/api/fleet/machines/kick", json={"roster_entry": _kick().to_dict()},
        )
    assert resp.status_code == 401


def test_kick_refuses_the_local_machine(env):
    client, state = env
    state.local_machine_id = TARGET_MID
    resp = _post(client, _kick(seq=1))
    assert resp.status_code == 400
    assert "machine you are using" in resp.json()["error"]
    assert state.stored == []


def test_kick_refuses_the_serving_machine(env):
    client, state = env
    state.selected_machine_id = TARGET_MID
    resp = _post(client, _kick(seq=1))
    assert resp.status_code == 400
    assert "serving the tunnel" in resp.json()["error"]
    assert state.stored == []


def test_kick_refuses_an_inactive_target(env):
    client, state = env
    state.entries = []  # nothing enrolled → target is not active
    resp = _post(client, _kick(seq=1))
    assert resp.status_code == 400
    assert "not currently active" in resp.json()["error"]
    assert state.stored == []


def test_kick_refuses_a_stale_seq(env):
    client, state = env
    state.entries = [_enroll(seq=3)]
    resp = _post(client, _kick(seq=3))  # does not beat the current seq 3
    assert resp.status_code == 400
    assert "does not beat" in resp.json()["error"]
    assert state.stored == []


def test_kick_refuses_a_bad_signature(env):
    client, state = env
    tampered = _kick(seq=1).to_dict()
    tampered["signature"] = "0" * 128
    resp = client.post(
        "/api/fleet/machines/kick", json={"roster_entry": tampered},
    )
    assert resp.status_code == 400
    assert state.stored == []


def test_kick_refuses_an_enroll_entry(env):
    client, state = env
    resp = _post(client, _enroll(seq=1))
    assert resp.status_code == 400
    assert "only a kick" in resp.json()["error"]
    assert state.stored == []


def test_kick_refuses_a_machine_id_that_disagrees_with_the_roster(env):
    client, state = env
    # Same public key (so it resolves) but a different, spoofed machine id.
    resp = _post(client, _kick(seq=1, machine_id="99" * 32, machine_pub=TARGET_PUB))
    assert resp.status_code == 400
    assert "does not match" in resp.json()["error"]
    assert state.stored == []
